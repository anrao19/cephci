import re
import secrets
import string
import time
import traceback

import dateutil.parser as parser

from ceph.ceph import CommandFailed
from tests.cephfs.cephfs_utilsV1 import FsUtils as FsUtilsv1
from tests.cephfs.cephfs_volume_management import wait_for_process
from tests.cephfs.snapshot_clone.cephfs_snap_utils import SnapUtils
from utility.log import Log

log = Log(__name__)


def run(ceph_cluster, **kw):
    """
    Workflows Covered : Validate Snapshot Schedule, Retention feature works on both volumes and subvolumes.
    Verify .snap across kernel, fuse and nfs mounts(future-ready) for snaps created by schedule.

    Type - Functional
    Workflow1 - snap_sched_vol: Verify Snapshot schedule on volume.Validate snaphots in .snap across
    mount types - kernel,fuse,nfs
    Steps:
    1. Create Snapshot schedule on ceph FS volume. Verify snapshot schedule created and active
    2. Add data to volume and wait for scheduled snapshots to be created
    3. Verify scheduled snapshtos are getting created.
    4. Validate snapshot schedule by checking if snapshots are created as per schedule.
    5. Verify snap list across all mount types - kernel,nfs,fuse
    Workflow2 - snap_sched_subvol: Verify Snapshot schedule on subvolume.Validate snaphots in .snap across
    mount types - kernel,fuse,nfs
    Steps : Repeat workflow1 steps on subvolume
    Workflow3 - snap_retention_vol: Verify Snapshot Retention on volume
    Steps:
    1. Create Snapshot schedule on ceph FS volume. Verify snapshot schedule created and active
    2. Create Snapshot retention policy on volume. Verify retention status is active.
    3. Add data to volume and wait for scheduled snapshots to be created
    4. Validate scheduled snapshots retained are as per retention policy.
    Workflow4 - snap_retention_subvol : Verify Snapshot Retention on subvolume
    Steps: Repeat workflow3 steps on subvolume.

    Type - Longevity
    (As high run time is acceptible in longevity setup, hourly schedule validation can be done. This test can
    be executed in parallel to scale tests in cephfs_scale suite on Baremetal.)
    Workflow5 - snap_sched_validate: Verify Snapshot schedule and Retention with hourly schedule.
    Workflow6 - snap_sched_snapcount_validate: Verify Snapshot schedule and retention for default snapshot count 50,
      non-default value say '75' and
    upto 'mds_max_snaps_per_dir' i.e., 100(default), 150(non-default). Verify no traceback with a snapshot creation
    when limit is reached. Verify with both manual and schedule snapshot creation(Add new minutely schedule to
    validate this)

    Type - Negative
    Workflow6 - snap_retention_service_restart: Verify Snapshot retention works even after service restarts.
    Verify for mgr, mon and mds.
    Workflow7 - snap_sched_non_existing_path: Verify Snapshot schedule can be created for non-existing path.
    After the start-time is hit for the
    schedule, the snap-schedule module should have deactivated the schedule without throwing a traceback error.
    The snap-scheule module should continue to remain responsive and a log message should be seen in the logs
    about deactivating the schedule.

    Type - Systemic
    Workflow8 - snap_sched_multi_fs: Create multiFS(3), Create snap-schedules across all FS except first one
    from the list obtained
    with ceph fs ls. Create snap-schedule with and without specifying FS name and validate behaviour, without
    FS name it should generate error. Remove snap-schedule with and without FS name and validate behaviour.

    Note : For each workflow except 5, all minutely, hourly, daily, weekly schedules will be applied and
    verified with status, but only minutely schedule is validated due to run time limitation.

    Clean Up:
    1. Del all the snapshots created
    2. Del Subvolumes
    3. Del SubvolumeGroups
    4. Deactivate and remove Snap_Schedule
    5. Remove FS

    """
    try:
        fs_util_v1 = FsUtilsv1(ceph_cluster)
        snap_util = SnapUtils(ceph_cluster)
        config = kw.get("config")
        clients = ceph_cluster.get_ceph_objects("client")
        build = config.get("build", config.get("rhbuild"))
        fs_util_v1.prepare_clients(clients, build)
        fs_util_v1.auth_list(clients)
        log.info("checking Pre-requisites")
        if len(clients) < 1:
            log.info(
                f"This test requires minimum 1 client nodes.This has only {len(clients)} clients"
            )
            return 1
        default_fs = "cephfs"
        nfs_servers = ceph_cluster.get_ceph_objects("nfs")
        nfs_server = nfs_servers[0].node.hostname
        nfs_name = "cephfs-nfs"
        client1 = clients[0]
        client1.exec_command(
            sudo=True, cmd=f"ceph nfs cluster create {nfs_name} {nfs_server}"
        )
        if wait_for_process(client=client1, process_name=nfs_name, ispresent=True):
            log.info("ceph nfs cluster created successfully")
        else:
            raise CommandFailed("Failed to create nfs cluster")
        nfs_export_name = "/export_" + "".join(
            secrets.choice(string.digits) for i in range(3)
        )
        snap_util.allow_minutely_schedule(client1, allow=True)
        fs_details = fs_util_v1.get_fs_info(client1, fs_name=default_fs)
        if not fs_details:
            fs_util_v1.create_fs(client1, default_fs)

        test_name_type = config.get("test_name", "all_tests")
        test_functional = [
            "snap_sched_vol",
            "snap_sched_subvol",
            "snap_retention_vol",
            "snap_retention_subvol",
        ]
        test_negative = [
            "snap_retention_service_restart",
            "snap_sched_non_existing_path",
        ]
        test_longevity = ["snap_sched_validate", "snap_sched_count_validate"]
        test_systemic = ["snap_sched_multi_fs"]
        test_name_all = test_functional + test_negative + test_longevity + test_systemic

        test_dict = {
            "all_tests": test_name_all,
            "functional": test_functional,
            "longevity": test_longevity,
            "systemic": test_systemic,
        }
        if test_name_type in test_dict:
            test_list = test_dict[test_name_type]
        else:
            test_list = [test_name_type]

        export_created = 0
        snap_test_params = {
            "ceph_cluster": ceph_cluster,
            "fs_name": default_fs,
            "nfs_export_name": nfs_export_name,
            "nfs_server": nfs_server,
            "export_created": export_created,
            "nfs_name": nfs_name,
            "fs_util": fs_util_v1,
            "snap_util": snap_util,
            "client": client1,
        }
        for test_case_name in test_list:
            log.info(
                f"\n\n                                   ============ {test_case_name} ============ \n"
            )
            snap_test_params.update({"test_case": test_case_name})
            cleanup_params = run_snap_test(snap_test_params)
            snap_test_params["export_created"] = cleanup_params["export_created"]
            if cleanup_params["test_status"] == 1:
                assert False, f"Test {test_case_name} failed"
            else:
                log.info(f"Test {test_case_name} passed \n")
        return 0
    except Exception as e:
        log.error(e)
        log.error(traceback.format_exc())
        return 1
    finally:
        log.info("Clean Up in progess")
        snap_util.allow_minutely_schedule(client1, allow=False)
        client1.exec_command(
            sudo=True,
            cmd=f"ceph nfs export delete {nfs_name} {nfs_export_name}",
            check_ec=False,
        )
        client1.exec_command(
            sudo=True,
            cmd=f"ceph nfs cluster delete {nfs_name}",
            check_ec=False,
        )


def run_snap_test(snap_test_params):
    test_case_name = snap_test_params["test_case"]
    # Create subvolume and group
    if "subvol" in snap_test_params.get("test_case"):
        subvolumegroup = {
            "vol_name": snap_test_params["fs_name"],
            "group_name": "subvolgroup_snap_schedule",
        }
        subvolume = {
            "vol_name": snap_test_params["fs_name"],
            "subvol_name": "snap_subvolume",
            "group_name": "subvolgroup_snap_schedule",
            "size": "6442450944",
        }
        snap_test_params["fs_util"].create_subvolumegroup(
            snap_test_params["client"], **subvolumegroup
        )
        snap_test_params["fs_util"].create_subvolume(
            snap_test_params["client"], **subvolume
        )
        snap_test_params["subvol_name"] = subvolume["subvol_name"]
        snap_test_params["group_name"] = subvolumegroup["group_name"]

    # Call test case modules
    if "snap_sched_vol" in test_case_name or "snap_sched_subvol" in test_case_name:
        post_test_params = snap_sched_test(snap_test_params)
    elif (
        "snap_retention_vol" in test_case_name
        or "snap_retention_subvol" in test_case_name
    ):
        post_test_params = snap_retention_test(snap_test_params)
    # Cleanup subvolume and group
    if "subvol" in snap_test_params.get("test_case"):
        cmd = f"ceph fs subvolume rm {snap_test_params['fs_name']} {snap_test_params['subvol_name']} "
        cmd += f"{snap_test_params['group_name']}"
        snap_test_params["client"].exec_command(
            sudo=True,
            cmd=cmd,
            check_ec=False,
        )
        snap_test_params["client"].exec_command(
            sudo=True,
            cmd=f"ceph fs subvolumegroup rm {snap_test_params['fs_name']} {snap_test_params['group_name']}",
            check_ec=False,
        )
    return post_test_params


def snap_sched_test(snap_test_params):
    snap_util = snap_test_params["snap_util"]
    fs_util = snap_test_params["fs_util"]
    client = snap_test_params["client"]
    snap_util.enable_snap_schedule(client)
    snap_test_params["path"] = "/"
    if "subvol" in snap_test_params.get("test_case"):
        cmd = f"ceph fs subvolume getpath {snap_test_params['fs_name']} {snap_test_params.get('subvol_name')} "
        cmd += f"{snap_test_params.get('group_name')}"
        subvol_path, rc = snap_test_params["client"].exec_command(
            sudo=True,
            cmd=cmd,
        )
        snap_test_params["path"] = f"{subvol_path.strip()}/.."
    snap_test_params["validate"] = True
    sched_list = ["2M", "1h", "1d", "1w"]
    mnt_list = ["kernel", "fuse", "nfs"]
    test_fail = 0
    mnt_paths = {}
    snap_list_type = {}
    post_test_params = {}
    for sched_val in sched_list:
        log.info(
            f"Running snapshot schedule test workflow for schedule value {sched_val}"
        )
        snap_test_params["sched"] = sched_val
        snap_test_params["start_time"] = get_iso_time(client)
        if snap_util.create_snap_schedule(snap_test_params) == 1:
            log.info("Snapshot schedule creation/verification failed")
            test_fail = 1
        if "M" in sched_val:
            sched_list = re.split(r"(\d+)", sched_val)
            duration_min = int(sched_list[1]) * 3
            for mnt_type in mnt_list:
                path, post_test_params["export_created"] = fs_util.mount_ceph(
                    mnt_type, snap_test_params
                )
                mnt_paths[mnt_type] = path
            snap_path = f"{mnt_paths['nfs']}"
            io_path = f"{mnt_paths['kernel']}/"
            deactivate_path = snap_test_params["path"]
            if "subvol" in snap_test_params.get("test_case"):
                snap_path = f"{mnt_paths['nfs']}{snap_test_params['path']}"
                io_path = f"{mnt_paths['kernel']}{subvol_path.strip()}/"
                deactivate_path = f"{subvol_path.strip()}/.."
            log.info(f"add sched data params : {client} {io_path} {duration_min}")
            snap_util.add_snap_sched_data(
                snap_test_params["client"], io_path, duration_min
            )
            if snap_util.validate_snap_schedule(client, snap_path, sched_val):
                log.info("Snapshot schedule validation failed")
                test_fail = 1
            snap_util.deactivate_snap_schedule(
                client, deactivate_path, sched_val=sched_val
            )
            time.sleep(10)
            log.info(
                "Verify if scheduled snaps in .snap are same across mount types - kernel,fuse and nfs"
            )
            for mnt_type in mnt_list:
                snap_path = mnt_paths[mnt_type]
                if "subvol" in snap_test_params.get("test_case"):
                    snap_path = f"{mnt_paths[mnt_type]}{subvol_path.strip()}/.."
                snap_list_type[mnt_type] = snap_util.get_scheduled_snapshots(
                    client, snap_path
                )
                if len(snap_list_type[mnt_type]) == 0:
                    log.info(
                        f"No scheduled snapshots listed in .snap - mount_type:{mnt_type},mount_path:{snap_path}"
                    )
                    test_fail = 1
            first_value = list(snap_list_type.values())[0]
            all_equal = all(value == first_value for value in snap_list_type.values())
            if all_equal is False:
                log.info(
                    "Scheduled snapshot list is not same across mount points - kernel, fuse, nfs"
                )
                test_fail = 1
    log.info(f"Performing test {snap_test_params['test_case']} cleanup")
    snap_util.deactivate_snap_schedule(snap_test_params["client"], deactivate_path)
    schedule_path = snap_test_params["path"]
    if "subvol" in snap_test_params.get("test_case"):
        schedule_path = f"{subvol_path.strip()}/.."
    post_test_params["test_status"] = test_fail
    snap_util.remove_snap_schedule(client, schedule_path)
    if "subvol" in snap_test_params.get("test_case"):
        for snap in snap_list_type["kernel"]:
            cmd = f"ceph fs subvolume snapshot rm {snap_test_params['fs_name']} {snap_test_params['subvol_name']} "
            cmd += f"{snap} {snap_test_params['group_name']}"
            client.exec_command(
                sudo=True,
                cmd=cmd,
                check_ec=False,
            )
    else:
        for snap in snap_list_type["fuse"]:
            client.exec_command(
                sudo=True,
                cmd=f"rmdir {mnt_paths['fuse']}/.snap/{snap}",
            )
    umount_all(mnt_paths, snap_test_params)
    return post_test_params


def snap_retention_test(snap_test_params):
    client = snap_test_params["client"]
    snap_util = snap_test_params["snap_util"]
    fs_util = snap_test_params["fs_util"]
    snap_util.enable_snap_schedule(client)
    snap_test_params["validate"] = True
    snap_test_params["path"] = "/"
    sched_list = ["2M", "1h", "7d", "4w"]
    snap_test_params["retention"] = "3M1h5d4w"
    test_fail = 0
    mnt_list = ["kernel", "fuse", "nfs"]
    mnt_paths = {}
    post_test_params = {}
    if "subvol" in snap_test_params.get("test_case"):
        cmd = f"ceph fs subvolume getpath {snap_test_params['fs_name']} {snap_test_params.get('subvol_name')} "
        cmd += f"{snap_test_params.get('group_name')}"
        subvol_path, rc = snap_test_params["client"].exec_command(
            sudo=True,
            cmd=cmd,
        )
        snap_test_params["path"] = f"{subvol_path.strip()}/.."
    for sched_val in sched_list:
        if "M" in sched_val:
            sched_val_ret = sched_val
        snap_test_params["sched"] = sched_val
        snap_test_params["start_time"] = get_iso_time(client)
        snap_util.create_snap_schedule(snap_test_params)
    snap_util.create_snap_retention(snap_test_params)
    ret_list = re.split(r"([0-9]+[ A-Za-z]?)", snap_test_params["retention"])
    snap_util.remove_snap_retention(client, snap_test_params["path"], ret_val="1h")
    snap_test_params["retention"] = "3M5d4w"
    for ret_item in ret_list:
        if "M" in ret_item:
            temp_list = re.split(r"(\d+)", ret_item)
            duration_min = int(temp_list[1]) * 2
            for mnt_type in mnt_list:
                path, post_test_params["export_created"] = fs_util.mount_ceph(
                    mnt_type, snap_test_params
                )
                mnt_paths[mnt_type] = path
            snap_path = f"{mnt_paths['nfs']}"
            io_path = f"{mnt_paths['kernel']}/"
            if "subvol" in snap_test_params.get("test_case"):
                snap_path = f"{mnt_paths['nfs']}{snap_test_params['path']}"
                io_path = f"{mnt_paths['kernel']}{subvol_path.strip()}/"
            log.info(f"add sched data params : {client} {io_path} {duration_min}")
            snap_util.add_snap_sched_data(client, io_path, duration_min)
            temp_list = re.split(r"(\d+)", sched_val_ret)
            wait_retention_check = int(temp_list[1]) * 60
            log.info(
                f"Wait for additional time {wait_retention_check}secs to validate retention"
            )
            time.sleep(wait_retention_check)
            if snap_util.validate_snap_retention(
                client, snap_path, snap_test_params["path"]
            ):
                test_fail = 1
    log.info(f"Performing test{snap_test_params['test_case']} cleanup")
    snap_list = snap_util.get_scheduled_snapshots(client, snap_path)
    post_test_params["test_status"] = test_fail
    schedule_path = snap_test_params["path"]
    snap_util.deactivate_snap_schedule(client, schedule_path)
    snap_util.remove_snap_retention(
        client, snap_test_params["path"], ret_val=snap_test_params["retention"]
    )
    snap_util.remove_snap_schedule(client, schedule_path)
    if "subvol" in snap_test_params.get("test_case"):
        for snap in snap_list:
            cmd = f"ceph fs subvolume snapshot rm {snap_test_params['fs_name']} {snap_test_params['subvol_name']} "
            cmd += f"{snap} {snap_test_params['group_name']}"
            client.exec_command(
                sudo=True,
                cmd=cmd,
                check_ec=False,
            )
    else:
        for snap in snap_list:
            client.exec_command(
                sudo=True,
                cmd=f"rmdir {mnt_paths['fuse']}/.snap/{snap}",
            )
    umount_all(mnt_paths, snap_test_params)
    return post_test_params


####################
# HELPER ROUTINES
####################
def umount_all(mnt_paths, umount_params):
    cmd = f"rm -rf {mnt_paths['kernel']}/*"
    if umount_params.get("subvol_name"):
        cmd = f"rm -rf {mnt_paths['kernel']}{umount_params['path']}/*"
    umount_params["client"].exec_command(sudo=True, cmd=cmd, timeout=1800)
    for mnt_type in mnt_paths:
        umount_params["client"].exec_command(
            sudo=True, cmd=f"umount {mnt_paths[mnt_type]}"
        )


def get_iso_time(client):
    date_utc = client.exec_command(sudo=True, cmd="date")
    log.info(date_utc[0])
    date_utc_parsed = parser.parse(date_utc[0])
    return date_utc_parsed.isoformat()