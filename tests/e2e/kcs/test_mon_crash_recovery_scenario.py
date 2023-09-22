import logging
import pytest
from random import choice

from ocs_ci.ocs.constants import (
    ROOK_CEPH_OPERATOR,
    CEPHBLOCKPOOL,
    STATUS_CLBO,
)
from ocs_ci.helpers.helpers import modify_deployment_replica_count
from ocs_ci.ocs.resources.deployment import get_mon_deployments
from ocs_ci.ocs.resources.pvc import get_pvc_objs
from ocs_ci.ocs.resources.pod import get_ceph_tools_pod, run_io_in_bg
from ocs_ci.ocs.resources.storage_cluster import ceph_mon_dump
from ocs_ci.framework.pytest_customization.marks import (
    tier3,
    skipif_external_mode,
    magenta_squad,
)
from ocs_ci.ocs.defaults import OCS_OPERATOR_NAME
from ocs_ci.helpers.helpers import wait_for_resource_state


log = logging.getLogger(__name__)


@magenta_squad
@tier3
@pytest.mark.skip(
    reason="Skip due to issue https://github.com/red-hat-storage/ocs-ci/issues/8531"
)
@pytest.mark.polarion_id("OCS-4942")
@pytest.mark.bugzilla("2151591")
@skipif_external_mode
class TestMonCrashRecoveryScenario:
    @pytest.fixture(autouse=True)
    def teardown_fixture(self, request):
        def scale_up_deployments():
            """Teardown function to scale deployments back to 1 replica."""
            for dep in [OCS_OPERATOR_NAME, ROOK_CEPH_OPERATOR]:
                log.info(f"Teardown: Scaling up {dep} to replica=1")
                modify_deployment_replica_count(dep, 1)

        request.addfinalizer(scale_up_deployments)

    def test_mon_crash_recovery_scenario(self, pod_factory, request):
        """
        Verifies system behavior when a crash occurs in the mon-x deployment.

        Steps:
            1. Select a random mon and courrupt the mon database.
            2. Start the IO workload in the background.
            3. Scale down the deployments of ocs-operator,rook-ceph-operator and rook-ceph-mon-a.
            4. Delete the Deployment of rook-ceph-mon-x and pvc rook-ceph-mon-x
            5. Scale up the operators to replicas = 1
            6. Verify 'ceph mon dump' command is working.
            7. Check for the any crash has generated.

        """

        mon_obj = choice(get_mon_deployments())
        mon_name = mon_obj.name
        mon_pvc = mon_obj.data["metadata"]["labels"]["pvc_name"]
        mon_pvc_obj = get_pvc_objs([mon_pvc])[0]

        # Step 1: Courrupting the mon database from the mon deployment.
        monpod = mon_obj.pods[0]
        monpod.exec_cmd_on_pod(
            f"rm -rf /var/lib/ceph/mon/ceph-{mon_name.split('-')[-1].strip()}",
            ignore_error=True,
        )
        wait_for_resource_state(resource=monpod, state=STATUS_CLBO)

        # Step 2:  Start IO Workload in the background.
        pod_obj = pod_factory(interface=CEPHBLOCKPOOL)
        run_io_in_bg(pod_obj)

        # Step 3: Scale down the deployments of ocs-operator,rook-ceph-operator and rook-ceph-mon-x.
        deployment_list = [OCS_OPERATOR_NAME, ROOK_CEPH_OPERATOR, mon_name]
        log.info(
            f"Scaling down deployments: {','.join(deployment_list)} to 0 replicas..."
        )
        for deployment in deployment_list:
            assert modify_deployment_replica_count(
                deployment, 0
            ), f"Fail to scale {deployment} to replica count: 0"

        # Step 4: Deleting the mon deployment
        log.info(f"Deleting mon deployment {mon_name}")
        mon_obj.delete()
        assert mon_obj.is_deleted, f"Mon Deployment {mon_name} is not deleted."

        # Step 5: Delete PVC associated with the MON.
        log.info(f"deleting pvc {mon_pvc_obj.name} associated with mon {mon_name}")
        mon_pvc_obj.delete()
        assert mon_pvc_obj.ocp.wait_for_delete(mon_pvc_obj.name)

        # Step 6: Scale up the deployment of  ocs-operator and rook-ceph-operator to replicas = 1
        log.info("Scaling up deployments to 1 replica...")
        for dep in [OCS_OPERATOR_NAME, ROOK_CEPH_OPERATOR]:
            assert modify_deployment_replica_count(
                dep, 1
            ), f"Failed to scale deployment {dep} to replicas : 1"

        # Step 7: Verify 'ceph mon dump' output has the recovered mon information.
        log.info(
            f"Verifying 'ceph mon dump' command output has information about recovered mon: {mon_name} "
        )
        mon_dump = ceph_mon_dump()
        assert [
            mon for mon in mon_dump["mons"] if mon["name"] == mon_name.split("-")[-1]
        ], f"'ceph mon dump' command output dont have the information about recovered mon: {mon_name}"

        # Step 8: Check If any crash has generated.
        log.info("Checking if the new crash has generated by the ceph.")
        toolbox = get_ceph_tools_pod()
        crash = toolbox.exec_ceph_cmd("ceph crash ls-new")
        assert not crash, f"Ceph cluster has generated crash {' '.join(crash[0])}"
