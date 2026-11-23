#!/usr/bin/env python3
"""Offline render acceptance using an operator-supplied Helm and local pinned chart.

Download chart 0.27.6 separately; this script neither downloads nor installs.
Temporary values are removed on success and failure. Never talks to a cluster.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import yaml
from h100_spot_controller.config import target_from_mapping
from h100_spot_controller.nth import nth_values, APP_VERSION
from tests.test_nth import fixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--helm', required=True)
    parser.add_argument('--chart', type=Path, required=True, help='local chart 0.27.6 tgz/directory')
    args = parser.parse_args()
    chart = args.chart.resolve(strict=True)
    meta = yaml.safe_load(subprocess.check_output([args.helm, 'show', 'chart', str(chart)], text=True))
    if meta.get('version') != '0.27.6' or str(meta.get('appVersion')) != '1.25.6':
        parser.error('only chart 0.27.6 / app 1.25.6 is accepted')
    with tempfile.TemporaryDirectory(prefix='nth-render-') as temp:
        work=Path(temp).resolve()
        env=dict(os.environ, KUBECONFIG=os.devnull, HELM_CACHE_HOME=str(work/'cache'),
                 HELM_CONFIG_HOME=str(work/'config'), HELM_DATA_HOME=str(work/'data'))
        target=target_from_mapping(fixture())
        for active in (False,True):
            values=nth_values(target,'us-east-1','1.32',enable_draining=active,acknowledge_cluster_rbac=active)
            file=work/'values.yaml'; file.write_text(yaml.safe_dump(values))
            subprocess.run([args.helm,'lint',str(chart),'-f',str(file)],check=True,env=env)
            rendered=subprocess.check_output([args.helm,'template','nth-gpu-demo',str(chart),
                '--namespace','kube-system','--kube-version','1.32.0','-f',str(file)],text=True,env=env)
            docs=[d for d in yaml.safe_load_all(rendered) if d]
            assert sorted(d['kind'] for d in docs)==['ClusterRole','ClusterRoleBinding','DaemonSet','ServiceAccount']
            spec=next(d for d in docs if d['kind']=='DaemonSet')['spec']['template']['spec']
            assert spec['hostNetwork'] is True
            assert spec['nodeSelector']==dict(values['linuxNodeSelector'],**{'kubernetes.io/os':'linux'})
            assert spec['tolerations']==values['linuxTolerations']
            container=spec['containers'][0]
            assert container['image'].endswith(':'+APP_VERSION)
            settings={i['name']:i.get('value') for i in container['env']}
            assert settings['DRY_RUN']==str(not active).lower()
            for key in ['ENABLE_SQS_TERMINATION_DRAINING','ENABLE_ASG_LIFECYCLE_DRAINING',
                        'ENABLE_SCHEDULED_EVENT_DRAINING','ENABLE_REBALANCE_MONITORING',
                        'ENABLE_REBALANCE_DRAINING','DELETE_LOCAL_DATA']:
                assert settings[key]=='false', key
            role=next(d for d in docs if d['kind']=='ClusterRole')
            assert not any('*' in r['verbs'] or '*' in r['resources'] or 'secrets' in r['resources'] for r in role['rules'])
            assert any('nodes' in r['resources'] and 'patch' in r['verbs'] for r in role['rules'])
            assert any('pods/eviction' in r['resources'] and 'create' in r['verbs'] for r in role['rules'])
            print('PASS: pinned chart render, dryRun='+str(not active))
    assert not work.exists(), 'render fixtures were not cleaned'
    print('Temporary render artifacts removed; live cluster acceptance NOT RUN')


if __name__ == '__main__':
    main()
