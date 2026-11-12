# 场景：区域推荐、初次选择和人工迁移

只适用通用 GPU。B300 固定 HYD；CPU 验证固定单区域，二者都拒绝跨区域迁移。SPS 不是库存承诺，也不是迁移审批。

## 1. 为每个候选区域准备输入

先读 [通用 GPU](generic-gpu.md)。不同于单区域路径，此处 candidate_regions 保留你已批准的多个区域，**每个区域都要有完整的 LT、AMI、profile、SG、AZ/subnet**。部署的 LaunchTemplateArns/LaunchInstanceRoleArns 要覆盖这些精确输入，以逗号分隔。不添加未经准备的区域碰碰运气。

选择固定的控制平面区域 REGION；状态表命令始终使用这个 REGION，即使机器迁到了另一处。

## 2. 选区域策略（不是换机器的策略）

| mode | 用法 | 容量已存在之后 |
|---|---|---|
| manual | 指定 active_region；SPS 仅参考 | 保持当前区域 |
| recommend | 记录推荐，但不据推荐自动切换 | 仍保持当前区域 |
| auto_initial | 所有候选区域都没有 owned Fleet/实例时，使用新鲜且合格的证据选一次 | 区域锁定，不因另一处得分高就迁移 |

先将 YAML disabled，改 mode，再用 `dry-run` 看 selected/recommended Region、排除原因、证据时间/版本。auto_initial 没有合格证据时等待，不创建机器。按通用指南部署并启用；不能在旧容量存在时删 state 强迫重新选择。

## 3. 迁移前先确认业务停机准备

整体迁移会产生容量空窗：先停源 Fleet 和源机器，确认源区域为零，再请求目的区域完整台数。不会两个区域并跑补短缺，也不保证目的区域能立即给机器。任务 checkpoint/恢复、EKS drain 和目标集群均由操作员准备。

配置需要两个已批准区域；source 必须 fully fulfilled，目的区域没有 owned 容量，launch contract 与价格检查通过。取得迁移审批前先核对这些。

## 4. 对健康 target 预览迁移计划

沿用部署指南的 READ_PROFILE、OP_PROFILE、TABLE、REGION：

```sh
export TARGET='gpu-demo'
export DESTINATION='REPLACE_APPROVED_DESTINATION_REGION'
.venv/bin/python -m h100_spot_controller.cli failover-request "$TARGET" \
  --destination-region "$DESTINATION" --table "$TABLE" --state-region "$REGION" --profile "$READ_PROFILE"
```

**检查点：** 源 Fleet/instance IDs、目的区域、完整目标台数、expiry、trigger=operator-request 均正确。此步骤不写 EC2。

## 5. 写入计划，再明确批准

计划写入会写 DynamoDB 并通知 SNS，但不立即改 EC2：

```sh
.venv/bin/python -m h100_spot_controller.cli failover-request "$TARGET" \
  --destination-region "$DESTINATION" --table "$TABLE" --state-region "$REGION" --profile "$OP_PROFILE" --execute
# 用返回的真实 plan_id，不要使用预览时的旧值：
export PLAN_ID='REPLACE_PERSISTED_PLAN_ID'
.venv/bin/python -m h100_spot_controller.cli failover-review "$TARGET" "$PLAN_ID" \
  --table "$TABLE" --state-region "$REGION" --profile "$READ_PROFILE"
```

业务停机/费用/权限负责人审批后：

```sh
.venv/bin/python -m h100_spot_controller.cli failover-approve "$TARGET" "$PLAN_ID" \
  --table "$TABLE" --state-region "$REGION" --profile "$OP_PROFILE" --execute
```

不要批准过期、配置已变化或不再匹配当前 Fleet 的计划。拒绝使用同样参数的 `failover-reject ... --execute`。

## 6. 观察执行和结束

定时 reconcile 领取审批，停止源 demand、终止计划中的机器，后续读确认 source=0，再创建目的 Fleet。capacity-review 加 --table 读取权威区域并枚举所有候选区域。保存先停后建的时间证据；目的区域短缺时保持等待，不重放旧审批。

持续短缺也可能生成容量短缺计划；用已返回的 plan_id 走第 5 步 review/approve，而非另作健康迁移请求。回迁要新计划、新审批。最后 [停机与清理](stop-and-cleanup.md)，不凭 YAML 原始 active_region 只清一个区域。
