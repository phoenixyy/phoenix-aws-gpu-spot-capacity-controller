# 场景：在一个区域维持 GPU Spot

**目标：** 从零部署一个 standalone controller，持续请求 1 台你选定的 GPU Spot。此路径不保证库存，不运行训练任务。多区域和 EKS 是后续独立场景。

## 1. 完成公共准备

先完成 [安装与输入准备](start-here.md)。以下命令是给获授权操作者的步骤，不是自动部署许可。准备 CDK CLI，确认 `cdk --version` 可用。账户首次使用 CDK 时，由管理员另行审核 bootstrap；本指南不自动执行 bootstrap。

## 2. 复制并填写配置

```sh
cp config/target.example.yaml privateinputs/gpu.yaml
```

编辑 `privateinputs/gpu.yaml`：

- `target_id: gpu-demo`，`enabled: false`，desired/maximum 都为 `1`。
- 明确 `acquisition_profile: generic`、`accelerator_profile: gpu-production`。
- `integration_mode: standalone`，`region_selection.mode: manual`。
- `active_region` 改为你的区域；`candidate_regions` **只保留该区域一项**，删掉未准备的第二区域。
- `instance_types` 填实际允许的 GPU 类型，添加你审核过的正数 `spot_price_cap_usd`。
- 填该区域的 LT ID、固定版本、AMI、instance-profile ARN、SG、bootstrap 版本、UserData hash、加密磁盘和标准 AZ 子网。
- 首次使用 `local_zone_placements: []`，不要误把测试 fixture 当你的基础设施。

Launch Template 的 instance 与 volume 都要有 `managed-by=h100-spot-capacity-controller`、`capacity-target-id=gpu-demo`、`Name=Phoenix-Codex-Local-Spot-gpu-demo`；bootstrap-contract-version 与 YAML 一致。其他自定义 ownership_tags 也要一致。启动模板的网络、驱动、任务启动脚本由你准备，不由 controller 安装。

## 3. 设置命令参数，执行只读检查

下列字符串必须替换，不要原样执行云端步骤：

```sh
export READ_PROFILE='REPLACE_READONLY_PROFILE'
export OP_PROFILE='REPLACE_OPERATOR_PROFILE'
export REGION='REPLACE_REGION'
export LT_ARN='REPLACE_EXACT_LAUNCH_TEMPLATE_ARN'
export INSTANCE_ROLE_ARN='REPLACE_EXACT_INSTANCE_ROLE_ARN'
export TABLE='Phoenix-Codex-Local-Spot-State'

.venv/bin/python -m h100_spot_controller.cli validate-target privateinputs/gpu.yaml
.venv/bin/python -m h100_spot_controller.cli discover privateinputs/gpu.yaml --profile "$READ_PROFILE"
.venv/bin/python -m h100_spot_controller.cli dry-run privateinputs/gpu.yaml --profile "$READ_PROFILE"
.venv/bin/python -m h100_spot_controller.cli capacity-review privateinputs/gpu.yaml --profile "$READ_PROFILE"
```

**检查点：** valid=true；机型 GPU 元数据正确；launch contract 合格；子网/zone/配额/价格证据可读；无旧 target 残余容量。`preflight` 命令只用于 B300/CPU，通用 GPU 使用 `dry-run`。退出码 0 也不能替代人工核对报告里的错误。

## 4. 部署控制平面（产生 AWS 资源和控制平面费用）

无 namespace 的默认 stack/物理名固定为 `Phoenix-Codex-Local-Spot-*`。同账户同区域已有这个部署时，**停止，不能把另一个 target 的部署当新 stack 覆盖**。多客户隔离详见 B300 手册；不要为了改名字随便替换已有资源。

```sh
cdk diff Phoenix-Codex-Local-Spot-Controller --app '.venv/bin/python app.py' \
  --profile "$OP_PROFILE" -c region="$REGION"
# 审核 IAM、资源替换、费用、LT/PassRole 精确范围后：
cdk deploy Phoenix-Codex-Local-Spot-Controller --app '.venv/bin/python app.py' \
  --profile "$OP_PROFILE" -c region="$REGION" \
  --parameters TargetId=gpu-demo \
  --parameters LaunchTemplateArns="$LT_ARN" \
  --parameters LaunchInstanceRoleArns="$INSTANCE_ROLE_ARN"
```

**检查点：** stack 部署成功；Lambda TARGET_ID=gpu-demo，状态表与区域匹配。SNS 邮件/系统订阅需另行配置并确认。此时尚未持久化 target，不能把定时调用找不到配置当获配失败。

## 5. 写入关闭配置

```sh
.venv/bin/python -m h100_spot_controller.cli target-put privateinputs/gpu.yaml \
  --table "$TABLE" --state-region "$REGION" --profile "$OP_PROFILE" --execute
.venv/bin/python -m h100_spot_controller.cli target-review gpu-demo \
  --table "$TABLE" --state-region "$REGION" --profile "$READ_PROFILE"
```

**检查点：** persisted enabled=false；记录输出的 configuration_version。配置已存在时先 review，不要删表或强行覆盖。

## 6. 启用 Spot 需求（开始可能的实例费用）

先批准台数/预算；编辑同一 YAML，**只把 enabled 改成 true**，重新 validate/dry-run。把最新 review 的版本写入下方变量：

```sh
export REVIEWED_VERSION='REPLACE_CONFIGURATION_VERSION'
.venv/bin/python -m h100_spot_controller.cli target-put privateinputs/gpu.yaml \
  --table "$TABLE" --state-region "$REGION" --profile "$OP_PROFILE" \
  --expected-version "$REVIEWED_VERSION" --execute --enable-capacity
```

**检查点：** enabled=true；之后 reconcile 日志可能是 healthy、shortfall、creation_pending 或明确错误。没有库存就等同一笔请求；不另建 Fleet、不删 token。UnauthorizedOperation 时停止启用动作并检查运行角色，不用管理员手动建 Fleet 冒充通过。

## 7. 观察并结束

```sh
.venv/bin/python -m h100_spot_controller.cli capacity-review privateinputs/gpu.yaml \
  --table "$TABLE" --state-region "$REGION" --profile "$READ_PROFILE"
```

确认实际 FleetId、实例 ID/类型和期望台数，CloudWatch FulfilledMachineCapacity 与日志匹配。要验证任务是否运行，另外检查你自己的 bootstrap/业务日志。

用完按照 [停机与清理](stop-and-cleanup.md) 取消 Fleet 并核对实例归零。通用路径没有 30 分钟自动停止。
