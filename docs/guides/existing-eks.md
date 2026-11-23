# 场景：为已有 EKS 提供 GPU EC2 容量

这不是 Karpenter。controller 不创建 EKS、不根据 pending Pod 扩缩容、不操作 Kubernetes、不保证节点注册。它只维护指定机器数量；你自己的启动模板把机器注册到已有集群。

## 1. 先检查是否适用

- 已有同区域 EKS 集群；管理员允许使用自管节点。
- 已准备 EKS 网络、节点 IAM/访问授权、GPU AMI、bootstrap、labels/taints。
- 能接受 Spot 丢机；任务恢复、调度和 checkpoint 由工作负载处理。

HYD B300 profile 不允许 existing-eks；本页只适用通用 GPU。想按 Pod 需求自动扩容请选 Kubernetes 原生方案。

## 2. 准备一个通用单区域 target

按 [通用 GPU 指南第 1–2 步](generic-gpu.md) 创建 privateinputs/gpu.yaml，但在部署/启用前改为：

根级设 `integration_mode: existing-eks`，并在对应 candidate_regions 的区域项中添加 `eks`。以下为 us-east-1 的结构示例；账号、集群、region、bootstrap/labels/taint 和 drain procedure 均按实际环境替换：

```yaml
# root: integration_mode: existing-eks
# candidate_regions 的 us-east-1 区域项内：
eks:
  cluster_arn: arn:aws:eks:us-east-1:ACCOUNT_ID:cluster/REPLACE_CLUSTER
  cluster_region: us-east-1
  bootstrap_contract_version: existing-eks-v1
  required_labels:
    capacity-target-id: gpu-demo
    topology.kubernetes.io/region: us-east-1
    topology.kubernetes.io/zone: REPLACE_AZ_NAME
    node.kubernetes.io/instance-type: REPLACE_GPU_TYPE
    capacity-source: spot
    spot-controller.phoenix.dev/nth-enabled: 'true'
  gpu_taint: 'nvidia.com/gpu=true:NoSchedule'
  source_drain_procedure: 'REPLACE_WITH_REVIEWED_DRAIN_RUNBOOK'
```

这是 bootstrap 合同，不是自动为节点设置标签的代码；你的 UserData 必须实现对应行为。不要用不存在的 `eks_cluster_name` 字段代替 `eks` 结构。

每个候选区域都必须有自己的同区域集群；首次只留一个区域。不要照搬别人的 cluster 名称、节点角色和安全组。

## 3. 审核启动模板

管理员确认实例 profile 中的节点角色可注册；AMI/驱动与机型匹配；bootstrap 指向正确集群；网络可到 API endpoint；标签、GPU taint 满足调度要求。controller 只核对 launch contract，不替代 EKS 注册测试。权限或安全设置有疑问停在这里审核，不自动放宽。

## 4. 接入节点中断处理，再启用容量

本仓库现在提供独立 [AWS Node Termination Handler 分步指南](eks-node-termination-handler.md) 和 `scripts/nth-values.py` 离线生成器。NTH IMDS DaemonSet 处理节点 cordon/drain；Fleet 继续负责机器补位。容量 Lambda 仍不调用 Kubernetes API，CDK 不自动安装 NTH。

先完成通用 GPU 第 3–5 步（只读检查、部署控制平面、写入 disabled），再按 NTH 指南核对版本/RBAC、生成 dry-run values、在明确批准的集群安装并审核激活。最后才走通用 GPU 第 6 步启用容量，并在第一批节点出现后检查 NTH 覆盖（没有节点时 DaemonSet desired=0 不能算验收）。

NTH pin 的兼容范围、opt-in 节点标签、GPU toleration、PDB/本地数据保护、验收和卸载全部在独立指南。它不保证捕捉手动 TerminateInstances；跨区迁移或人工清理仍先明确 drain。已有其他节点终止管理器时先审核，不重复部署。

**检查点分两层：**

1. capacity-review 和 reconcile healthy：EC2 Fleet 满足机器台数。
2. 由你使用已授权 Kubernetes 工具确认节点 Ready、GPU 可调度，运行自己的验证负载。

第 1 层通过而第 2 层失败，优先排查 bootstrap/网络/EKS 访问/驱动；不要增加 desired_count 掩盖注册失败。

## 5. 可选：发布 readiness 观察值

外部观察器可向 controller 状态表写 `pk=TARGET#<target_id>`、`sk=EKS_READINESS#<region>`，字段 registered_node_count、ready_node_count、observed_at。这个仓库不提供自动 Kubernetes 采集器；需单独实现和批准权限。指标只观测，不反向调整 Fleet 数量。格式说明见 [运维手册](../operator-runbook.md)。

## 6. 结束使用

先保存任务进度并 drain/清理 Node，随后 [停机与清理](stop-and-cleanup.md)。跨区域迁移还需人工处理源集群 workload 和 Node，不会自动搬 Pod；见 [区域迁移](regions.md)。
