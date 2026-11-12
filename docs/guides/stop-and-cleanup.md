# 结束使用：暂停、取消需求、终止实例

所有场景最后都要走这页。**删 stack、关 Lambda 或 enabled=false 均不等于取消 maintain Fleet。** 云端命令只有已授权的操作者才能执行；不明写入、所有权不匹配时停止删除。

## 1. 指定自己的配置和控制平面

以下是通用 GPU 的命名示例。B300 改为自己的客户配置/target/table 和 ap-south-2。CPU 保留原 run/start/expiry，不改时间续期。

```sh
export CONFIG='privateinputs/gpu.yaml'
export TARGET='gpu-demo'
export TABLE='Phoenix-Codex-Local-Spot-State'
export REGION='REPLACE_STATE_TABLE_REGION'
export READ_PROFILE='REPLACE_READONLY_PROFILE'
export OP_PROFILE='REPLACE_OPERATOR_PROFILE'

.venv/bin/python -m h100_spot_controller.cli target-review "$TARGET" \
  --table "$TABLE" --state-region "$REGION" --profile "$READ_PROFILE"
```

**检查点：** 这是你的 target 和状态表，记下最新 configuration_version。迁移后的实际容量区域以状态表为准，REGION 此处始终是状态表区域。

## 2. 暂停控制器的容量写入

编辑 CONFIG 对应 YAML，只把 enabled 改 false。使用第 1 步最新版本：

```sh
export REVIEWED_VERSION='REPLACE_CONFIGURATION_VERSION'
.venv/bin/python -m h100_spot_controller.cli target-put "$CONFIG" \
  --table "$TABLE" --state-region "$REGION" --profile "$OP_PROFILE" \
  --expected-version "$REVIEWED_VERSION" --execute
.venv/bin/python -m h100_spot_controller.cli target-review "$TARGET" \
  --table "$TABLE" --state-region "$REGION" --profile "$READ_PROFILE"
```

**检查点：** 持久配置 enabled=false；等已有 writer 结束。writer_busy 时不要删锁或状态强行执行。现有 Fleet 仍会补位、仍可能花钱。

## 3. 预览要处理哪些资源

```sh
.venv/bin/python -m h100_spot_controller.cli cleanup "$CONFIG" \
  --table "$TABLE" --state-region "$REGION" --profile "$READ_PROFILE"
```

核对 FleetId 和实例 IDs。预览为空而 creation intent 未决、saved FleetId 不可见时不能当清理成功。保留请求身份，查服务端结果，不重新启用新 generation。

## 4. 选择停止方式（两种选一）

**A. 只取消后续补位，保留现有机器：**

```sh
.venv/bin/python -m h100_spot_controller.cli cleanup "$CONFIG" \
  --table "$TABLE" --state-region "$REGION" --profile "$OP_PROFILE" --execute
```

机器仍会计费，要另行安排终止。

**B. 取消需求并终止该 target 的机器：** 停训练、保存 checkpoint、EKS drain 等业务操作先完成，再取得不可逆终止授权。

```sh
.venv/bin/python -m h100_spot_controller.cli cleanup "$CONFIG" \
  --table "$TABLE" --state-region "$REGION" --profile "$OP_PROFILE" --execute --terminate-instances
```

命令返回的是 API 结果，不保证当场停止计费。等待 EC2 终态。

## 5. 核实清理结果

```sh
.venv/bin/python -m h100_spot_controller.cli capacity-review "$CONFIG" \
  --table "$TABLE" --state-region "$REGION" --profile "$READ_PROFILE"
```

选 B 时确认所有候选区域：无 active owned Fleet、无非 terminated owned 实例。EC2 控制台另查 run-owned EBS、弹性 IP/公网费用和其他保留资源；capacity-review 不枚举这些。Fleet deleted 历史条目不等于仍有需求。

## 6. 是否删除控制平面

可保留供后续使用，但同一 CPU run 不可重启；新测试需新身份与审批。要删 stack 时先完成容量归零检查，再保存日志与必要恢复证据，由操作者审核 `cdk destroy`。

生产 CDK 的 DynamoDB、SNS 与日志是 RETAIN，destroy 后可能仍存在并产生费用。实例 LT、SG、AMI、role/profile 不属于 controller stack，不会自动删。不要删除共享网络、账户级 service-linked role 或其他客户资源。完整保留项见 [运维手册](../operator-runbook.md)。
