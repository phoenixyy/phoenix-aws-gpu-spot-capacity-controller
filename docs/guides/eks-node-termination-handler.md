# EKS 自管 Spot 节点：接入 AWS Node Termination Handler

本仓库提供 NTH 的**独立、可审查 IMDS Helm 集成**。EC2 Fleet 负责补位；NTH 负责收到中断信号后 cordon/drain；Kubernetes/业务负责重新调度与任务恢复。生成配置不会部署，CDK 也不会自动安装 NTH。

## 0. 适用范围与版本闸门

- 只适用 `generic` + `gpu-production` + `existing-eks` 的 Linux 自管 Spot 节点。B300 standalone/CPU 不适用。
- 此次固定 **Helm chart 0.27.6 / NTH v1.25.6**，依据 [该 release 的 Chart.yaml](https://github.com/aws/aws-node-termination-handler/blob/v1.25.6/config/helm/aws-node-termination-handler/Chart.yaml)。不是宣称最新版本。
- 当前生成器只接受 upstream compatibility 表列出的 Kubernetes **1.29–1.32**。其他版本须先审查新 release 并更新 pin/渲染测试，不绕过生成器的版本闸门。生产使用还需确认该 EKS 版本处于支持期。
- 节点已被 Managed Node Group、Karpenter、Auto Mode 或其他终止组件管理时，不按本页再装另一套；先核对已有中断处理，避免两个处理器重复 drain。
- 本次仓库验收只有离线值生成和固定 chart 渲染；真实 EKS/Spot 中断验收 **NOT RUN**。

官方行为见 [NTH README](https://github.com/aws/aws-node-termination-handler)。

## 1. 安装前核对权限、网络和业务

此页后续 helm upgrade/kubectl label 都会改真实集群，必须另行取得该集群权限和业务批准。以下不是本次代码发布的部署授权。

| 项目 | 必须审核的内容 |
|---|---|
| 节点 ownership | 仅这个 Fleet 的自管 Linux Spot；不是集群里所有节点 |
| Kubernetes RBAC | upstream ClusterRole 可 get/list/patch/update nodes，get/list pods，create pods/eviction，get daemonsets；**权限是集群级，不能凭 nodeSelector 认为授权只限 target** |
| IMDSv2 | 保持 EC2 HttpTokens=required。NTH hostNetwork=true，通过宿主网络访问 IMDS；不为此降低 IMDS 安全设置或加全局凭据 |
| Pod 安全策略 | upstream Pod 使用 hostNetwork 和只读 /proc/uptime hostPath；安全负责人确认所在 namespace/admission 允许；不自动放宽 namespace 策略 |
| 网络/镜像 | 节点可拉 public.ecr.aws 固定版本镜像，并可到 Kubernetes API。私网环境请先准备经审核的镜像分发/网络路径，不临时新建 NAT |
| 工作负载 | 审核 PDB、terminationGracePeriodSeconds、SIGTERM、checkpoint、空盘数据。drain 成功不代表训练恢复 |

IMDS 模式不增加 AWS IAM/IRSA/SQS。DaemonSet 的 ServiceAccount 需要 Kubernetes API token；hostNetwork 仍可能访问实例角色凭据，这不是“完全没有权限”的 Pod。

## 2. 准备节点标签与 taint 合同

使用 [已有 EKS 指南](existing-eks.md) 的 target YAML。假定 target_id=gpu-demo、候选区域 us-east-1（实际使用替换）。在该区域 `eks.required_labels` 中确认：

```yaml
capacity-target-id: gpu-demo
capacity-source: spot
spot-controller.phoenix.dev/nth-enabled: 'true'
```

bootstrap 必须让**后续补位节点**也带这些 Kubernetes 标签。EC2 tags 不能代替 Kubernetes labels。GPU taint 的实际值必须与配置里的 `eks.gpu_taint` 匹配。生成器只 tolerates 这一个明确的 GPU taint，不用 blanket Exists 绕过其他隔离 taint。

现有节点仅在管理员核对 providerID 对应的 EC2 实例确属本 target/Fleet 后才能加 opt-in 标签：

```sh
export CONTEXT='REPLACE_EXACT_KUBECONFIG_CONTEXT'
export NODE='REPLACE_VERIFIED_TARGET_NODE'
kubectl --context "$CONTEXT" get node "$NODE" -o yaml
# 核对 labels、spec.providerID 和 spec.taints；不得对所有节点批量打标。
kubectl --context "$CONTEXT" label node "$NODE" spot-controller.phoenix.dev/nth-enabled=true
```

不要在已有其他终止管理器的节点上打该 opt-in 标签。设置标签不会自动修改 EC2 Fleet target_count。

## 3. 生成默认 dry-run 配置并渲染（只在本地）

确认真实集群 Kubernetes minor，例如获授权只读 `kubectl --context "$CONTEXT" version -o yaml`。以下 1.32 仅示例，不猜集群版本。

```sh
export CONFIG='privateinputs/gpu.yaml'
export NTH_REGION='us-east-1'
export K8S_MINOR='1.32'
export RELEASE='nth-gpu-demo'  # 每个 target 独立 release；不要覆盖既有 NTH
export NAMESPACE='kube-system'
export CHART='oci://public.ecr.aws/aws-ec2/helm/aws-node-termination-handler'
export CHART_VERSION='0.27.6'

.venv/bin/python scripts/nth-values.py "$CONFIG" \
  --region "$NTH_REGION" --kubernetes-version "$K8S_MINOR" > privateinputs/nth-review.yaml
helm template "$RELEASE" "$CHART" --version "$CHART_VERSION" \
  --namespace "$NAMESPACE" --kube-version "$K8S_MINOR.0" \
  -f privateinputs/nth-review.yaml > privateinputs/nth-rendered.yaml
```

**检查点：** dryRun=true；只有 Linux DaemonSet、SA、ClusterRole/Binding；无 Deployment/queue/AWS IAM；selectors 同时有 target、Spot 和 opt-in；GPU toleration 具体匹配；镜像 v1.25.6。审查渲染文件中的 RBAC，而不只看 values。

安全默认：Spot interruption 开启；ASG/scheduled/rebalance 关闭；deleteLocalData=false；不使用 out-of-service taint；不覆盖 Pod 自有退出 grace。120 秒只是处理配置，不延长 EC2 的回收时间或保证 drain 有足够时间。

## 4. 获授权后安装 dry-run release（真实集群写）

先 `helm --kube-context "$CONTEXT" list -A` 和只读检查已有 NTH/Karpenter 等组件，确认本 release 不覆盖现有安装、目标节点没有其他中断处理器。不要使用 default context。

```sh
helm upgrade --install "$RELEASE" "$CHART" --version "$CHART_VERSION" \
  --kube-context "$CONTEXT" --namespace "$NAMESPACE" \
  -f privateinputs/nth-review.yaml --atomic --wait --timeout 5m
kubectl --context "$CONTEXT" -n "$NAMESPACE" get daemonsets \
  -l "app.kubernetes.io/instance=$RELEASE" -o wide
kubectl --context "$CONTEXT" -n "$NAMESPACE" get pods \
  -l "app.kubernetes.io/instance=$RELEASE" -o wide
kubectl --context "$CONTEXT" -n "$NAMESPACE" logs \
  -l "app.kubernetes.io/instance=$RELEASE" --tail=100
```

**检查点：** 每个匹配的 Ready 目标节点有 NTH Pod，非目标节点没有；desired=0 不算覆盖成功。没有节点时可先装 DS，但 Fleet 获配后必须补核对。确认无 IMDSv2/API/镜像/Pod admission 错误。dryRun 只记录节点操作，不会真实 cordon/drain；没出现中断事件也不能声称完整验收。

## 5. 单独批准实际 cordon/drain 后启用

先审核业务影响、RBAC 和节点范围。激活的确认开关不是自动审批系统：

```sh
.venv/bin/python scripts/nth-values.py "$CONFIG" \
  --region "$NTH_REGION" --kubernetes-version "$K8S_MINOR" \
  --enable-draining --acknowledge-cluster-rbac > privateinputs/nth-active.yaml
helm template "$RELEASE" "$CHART" --version "$CHART_VERSION" \
  --namespace "$NAMESPACE" --kube-version "$K8S_MINOR.0" \
  -f privateinputs/nth-active.yaml > privateinputs/nth-active-rendered.yaml
# 审核 dryRun=false 且 selectors/RBAC 未扩大，然后才执行：
helm upgrade "$RELEASE" "$CHART" --version "$CHART_VERSION" \
  --kube-context "$CONTEXT" --namespace "$NAMESPACE" \
  -f privateinputs/nth-active.yaml --atomic --wait --timeout 5m
```

Spot interruption notice 到来时，NTH 尝试 cordon → 按 eviction/PDB drain。Fleet 在实例终止后尝试补位，替代节点的 bootstrap 再注册。没有启动新节点的专门 surge，也没有训练续跑功能。

Rebalance recommendation 可能没有实际回收。在固定目标 Fleet 上提前 cordon/drain 会让节点不再可调度但机器仍占 target_count，所以本集成默认**不监听、不 drain rebalance**；原控制器可继续发该信号的告警。

## 6. 真实验收与失败边界

只有独立批准的隔离验收才测试中断；本指南不自动触发 FIS、EC2 termination 或伪造生产 IMDS endpoint。

应保留：目标节点 providerID/实例 ID；NTH 收到 interruption 日志；节点 unschedulable 变化；eviction/drain 结果；任务保存进度证据；同 Fleet 新实例和 Ready 节点；非目标节点未操作。

- 手动 EC2 TerminateInstances **不是** IMDS Spot interruption notice。此模式不保证捕捉手动终止；运维/跨区迁移应先明确 drain，不能立即终止然后等待 NTH 救场。
- PDB 可能阻止 eviction；deleteLocalData=false 会阻止带 emptyDir 数据的 Pod 被默认强删。若业务明确接受本地数据丢失，需另行批准 values 改动再渲染审核，不偷偷开启。
- 短通知、驱动初始化、业务长 shutdown 都可能让 drain 或恢复来不及；NTH 不阻止 AWS 最终回收。
- 仅 Fleet healthy 或 DS Ready 不能证明真实 interruption/drain 已验证。

## 7. 暂停、卸载与清理

回退到步骤 4 的 dry-run values 可停止后续真实节点操作。它不会取消已在进行的操作，也不会自动 uncordon 已被 cordon 的节点。由操作者逐个确认节点未待回收、业务状态安全后才处理 uncordon；不要批量恢复所有节点。

卸载仅删除自己的 release：

```sh
helm uninstall "$RELEASE" --kube-context "$CONTEXT" --namespace "$NAMESPACE" --wait --timeout 5m
```

核对该 release 的 DS/SA/ClusterRole/Binding 消失；保留其他 target/团队的组件。先从未来 bootstrap 删除 opt-in，再逐个移除本 target 的 opt-in 标签；不要移除共享标签或 GPU taint。**卸载 NTH 不会取消 Fleet/终止实例**，费用清理仍按 [停机清理](stop-and-cleanup.md)。

## 版本与离线验证依据

版本、values、Linux DS 和 RBAC 参考固定 upstream [v1.25.6](https://github.com/aws/aws-node-termination-handler/tree/v1.25.6/config/helm/aws-node-termination-handler)。本地可运行 `scripts/release-check.py` 覆盖生成器合同；维护者还用真实 chart 0.27.6 做 dry-run/active 两种 Helm 渲染，确认调度范围与默认宽 toleration 已替换。验证时 OCI chart digest 为 `sha256:3c31997d84ea4aef1c4860feb3fce70f95d45ce71a9758ffcdc504af5fbb75b4`；若同版本获取了不同内容，停止升级并核对来源。

维护者复现渲染检查（需要已有 Helm，下载 chart 但不安装集群）：

```sh
mkdir -p testoutputs/nth
helm pull "$CHART" --version "$CHART_VERSION" --destination testoutputs/nth
.venv/bin/python -B scripts/check-nth-render.py --helm helm \
  --chart testoutputs/nth/aws-node-termination-handler-0.27.6.tgz
```

检查脚本自身的临时 values/cache 会自动清理；下载的 chart 是操作者显式保留的测试输入，用完核对归属后删除，不提交。升级 chart 必须重做渲染测试；不要只替换镜像 tag。
