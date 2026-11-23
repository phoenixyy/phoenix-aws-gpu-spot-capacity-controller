"""NTH integration unit tests; no SDK, Helm install or cluster contact."""
from copy import deepcopy
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml
from h100_spot_controller.config import ConfigurationError, target_from_mapping
from h100_spot_controller.nth import APP_VERSION, CHART_VERSION, OPT_IN_LABEL, nth_values

ROOT = Path(__file__).resolve().parents[1]


def fixture():
    raw = yaml.safe_load((ROOT / 'config/target.example.yaml').read_text())
    raw.update(target_id='gpu-demo', integration_mode='existing-eks')
    raw['candidate_regions'] = raw['candidate_regions'][:1]
    raw['candidate_regions'][0]['eks'] = {
        'cluster_arn': 'arn:aws:eks:us-east-1:111122223333:cluster/test',
        'cluster_region': 'us-east-1', 'bootstrap_contract_version': 'existing-eks-v1',
        'required_labels': {'capacity-target-id': 'gpu-demo', 'capacity-source': 'spot',
                            'topology.kubernetes.io/region': 'us-east-1',
                            'topology.kubernetes.io/zone': 'us-east-1a',
                            'node.kubernetes.io/instance-type': 'p5.48xlarge'},
        'gpu_taint': 'nvidia.com/gpu=true:NoSchedule',
        'source_drain_procedure': 'reviewed-runbook',
    }
    return raw


class NthTests(unittest.TestCase):
    def values(self, raw=None, **kwargs):
        return nth_values(target_from_mapping(raw or fixture()), 'us-east-1', '1.32', **kwargs)

    def test_review_default_no_clients(self):
        with patch('socket.socket', side_effect=AssertionError('network forbidden')):
            result = self.values()
        self.assertTrue(result['dryRun'])
        self.assertFalse(result['enableSqsTerminationDraining'])
        self.assertTrue(result['useHostNetwork'])
        self.assertEqual(result['image']['tag'], APP_VERSION)
        self.assertEqual(CHART_VERSION, '0.27.6')

    def test_selectors_and_exact_gpu_toleration(self):
        values = self.values()
        self.assertEqual(values['linuxNodeSelector'], {
            'capacity-target-id': 'gpu-demo', 'capacity-source': 'spot', OPT_IN_LABEL: 'true'})
        self.assertEqual(values['daemonsetTolerations'], [])
        self.assertEqual(values['linuxTolerations'], [
            {'key': 'nvidia.com/gpu', 'operator': 'Equal', 'value': 'true', 'effect': 'NoSchedule'}])

    def test_real_draining_requires_acknowledgement(self):
        with self.assertRaises(ConfigurationError):
            self.values(enable_draining=True)
        self.assertFalse(self.values(enable_draining=True, acknowledge_cluster_rbac=True)['dryRun'])
        self.assertTrue(self.values(acknowledge_cluster_rbac=True)['dryRun'])

    def test_no_early_drain_surge_or_local_data_loss(self):
        values = self.values(enable_draining=True, acknowledge_cluster_rbac=True)
        for key in ('enableASGLifecycleDraining', 'enableScheduledEventDraining',
                    'enableRebalanceMonitoring', 'enableRebalanceDraining',
                    'deleteLocalData', 'enableOutOfServiceTaint'):
            self.assertFalse(values[key])
        self.assertEqual(values['serviceAccount']['annotations'], {})
        self.assertNotIn('queueURL', values)
        self.assertNotIn('awsAccessKeyID', values)

    def test_reject_standalone_cpu_and_hyd(self):
        raw = fixture()
        raw['integration_mode'] = 'standalone'
        raw['candidate_regions'][0].pop('eks')
        with self.assertRaises(ConfigurationError): self.values(raw)
        for name in ('hyd-b300.example.yaml', 'cpu-validation.example.yaml'):
            raw = yaml.safe_load((ROOT/'config'/name).read_text())
            with self.assertRaises(ConfigurationError): self.values(raw)

    def test_region_and_supported_version(self):
        target = target_from_mapping(fixture())
        with self.assertRaises(ConfigurationError): nth_values(target, 'us-west-2', '1.32')
        with self.assertRaises(ConfigurationError): nth_values(target, 'us-east-1', '1.99')
        for minor in ('1.29', '1.30', '1.31', '1.32'):
            self.assertTrue(nth_values(target, 'us-east-1', minor)['dryRun'])

    def test_mismatched_labels_and_bad_taint_rejected(self):
        for key, value in [('capacity-source', 'on-demand'), ('capacity-target-id', 'other')]:
            raw=fixture(); raw['candidate_regions'][0]['eks']['required_labels'][key]=value
            with self.assertRaises(ConfigurationError): self.values(raw)
        raw=fixture(); raw['candidate_regions'][0]['eks']['gpu_taint']='*'
        with self.assertRaises(ConfigurationError): self.values(raw)
        raw=fixture(); raw['target_id']='x'*64
        raw['candidate_regions'][0]['eks']['required_labels']['capacity-target-id']=raw['target_id']
        with self.assertRaises(ConfigurationError): self.values(raw)

    def test_does_not_modify_target_or_require_enabled(self):
        raw=fixture(); before=deepcopy(raw)
        self.values(raw)
        self.assertEqual(raw, before)
        self.assertFalse(raw['enabled'])

    def test_cli_produces_values_without_deployment(self):
        with tempfile.TemporaryDirectory(prefix='nth-unit-') as temp:
            path=Path(temp)/'target.yaml'; path.write_text(yaml.safe_dump(fixture()))
            result=subprocess.run([sys.executable, '-B', str(ROOT/'scripts/nth-values.py'), str(path),
                                   '--region','us-east-1','--kubernetes-version','1.32'],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertTrue(yaml.safe_load(result.stdout)['dryRun'])
            self.assertIn('version=0.27.6',result.stdout)
        self.assertFalse(Path(temp).exists())
