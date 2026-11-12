# Hyderabad B300 Spot 客户交付（0.2.0）

> **当前 HYD/B300 实际履约、中断后补位、客户账户 preflight、部署：NOT RUN。** 本次交付只完成离线实现/验证，不代表 ap-south-2 现在有可分配的 B300 Spot。通用 Seoul G6e 历史验证不能替代 B300 证据。下面所有 AWS 读操作必须先获得客户账户只读授权；所有写操作都只是待授权命令示例，本次未执行。

## 1. 固定边界与持续补位

- `acquisition_profile: hyd-b300`，只允许 `ap-south-2`、`p6-b300.48xlarge`、`gpu-production`、manual Region、standalone；元数据必须验证 NVIDIA B300 / 8 GPU / Spot usage class。
- 示例 `enabled: false`，desired/maximum 都为 **1 台机器**（不是 1 个 GPU）。`all-approved` 将审核后的标准 AZ 放入**同一个 maintain Fleet**，总量仍是 1，不是每 AZ 1 台。禁止 Local Zones、跨 Region、On-Demand fallback、rebalance surge。
- AWS maintain Fleet 在 Lambda 两次轮询之间仍会持续请求容量。中断后 AWS 会尝试重新满足同一请求；短缺可以长期存在。controller 观察实际 Fleet/实例、发布短缺/错误并保留请求，不通过频繁删建 Fleet “抢容量”。
- 只适合可独立运行的训练/推理任务；**不实现 checkpoint/resume、任务恢复/调度、DDP/gang placement、跨 AZ 训练拓扑或 EKS 集成**。机器重新出现不等于业务恢复、GPU 驱动就绪或训练进度恢复。
- Spot ceiling 为 `min(唯一有效 Linux On-Demand 小时价, 已配置客户每类型 Spot cap)`；不配置客户 cap 时仍使用 fail-closed On-Demand ceiling。0.2.0 修正此前静态 cap 被忽略的行为。价格上限不能提高竞价优先级，也不能保证折扣或库存。

## 2. 两个客户必须独立

| 输入 | 客户 A 示例 | 客户 B 示例 |
|---|---|---|
| CDK `customer_id` / deployment namespace | `customer-a` | `customer-b` |
| target_id / CfnParameter TargetId | `customer-a-hyd-b300` | `customer-b-hyd-b300` |
| 配置 ownership_tags.customer-id | `customer-a` | `customer-b` |
| 独立状态表 | `Phoenix-Codex-Local-Spot-customer-a-State` | `Phoenix-Codex-Local-Spot-customer-b-State` |
| 独立 stack | `Phoenix-Codex-Local-Spot-customer-a-Controller` | `Phoenix-Codex-Local-Spot-customer-b-Controller` |
| 账户 profile 示例 | `customer-a-readonly` / `customer-a-operator` | `customer-b-readonly` / `customer-b-operator` |

namespace 必须为 1–12 位小写字母/数字/连字符，首尾只能字母或数字，不做静默截断或替换；同账户同 Region 的客户必须不同 namespace。配置 customer-id 和 target_id 必须与部署运行环境 `CUSTOMER_ID` / `TARGET_ID` 完全匹配。CLI 保留兼容名称 `h100-spot-controller`，Python 模块入口为 `python -m h100_spot_controller.cli`。**无 namespace 的旧部署物理名称不变**；不要把已运行的 legacy stack 原地改成客户 namespace（那是资源替换，不是重命名）。

从 [禁用 HYD 示例](<../config/hyd-b300.example.yaml>) 各复制一份到未跟踪的 `privateinputs/`，客户 A/B 分别修改 `target_id`、`ownership_tags.customer-id`、Name tag、Region 内审核的 AMI/Launch Template/version/子网/安全组/实例角色/bootstrap 合约和客户预算。填好的输入不得提交 Git、打包或混用 credentials。Lambda asset 明确排除 config/privateinputs、.agents/openspec/scripts/testoutputs；排除 asset 不等于 Git 忽略，提交前仍检查状态。

## 3. 本地检查优先（不访问 AWS）

先准备 Python 3.11+ 和 CDK 支持的 Node.js（本轮使用 Python 3.12 / Node 24）。在源码目录安装锁定依赖和项目入口；这些是本地依赖安装，不调用 AWS：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python -m h100_spot_controller.cli --help
.venv/bin/python -m h100_spot_controller.cli validate-target config/hyd-b300.example.yaml
.venv/bin/python scripts/release-check.py
# 若已安装 OpenSpec，可追加严格变更验证
.venv/bin/python scripts/release-check.py --openspec
```

release-check 使用假凭据、关闭 EC2 metadata，阻断 botocore endpoint HTTP（包括自定义 endpoint），保留 CDK jsii 的本地 Node 通信；运行 unittest 和 disabled examples 校验，不执行账户发现。合成/fixture 使用 TemporaryDirectory teardown，不在项目留下 cdk.out。正常 `.venv` 是持久依赖环境，不是清理对象。

## 4. 获授权后：账户只读证据（不创建容量）

先由客户准备只读 profile；本交付不登录、不查看凭据。A/B 分别执行以下命令（B 替换对应路径、profile、table）。所有状态表命令显式带 `--state-region ap-south-2`；HYD 文件型命令还会强制校验该区域，避免 profile 默认 Region 指向其他区域。读取 target-review 时也必须带此参数。

```sh
.venv/bin/python -m h100_spot_controller.cli validate-target privateinputs/customer-a.yaml
.venv/bin/python -m h100_spot_controller.cli discover privateinputs/customer-a.yaml --profile customer-a-readonly
.venv/bin/python -m h100_spot_controller.cli dry-run privateinputs/customer-a.yaml --profile customer-a-readonly
.venv/bin/python -m h100_spot_controller.cli capacity-review privateinputs/customer-a.yaml --profile customer-a-readonly
# stack 已存在时，加入自己的状态表读取权威 active Region
.venv/bin/python -m h100_spot_controller.cli capacity-review privateinputs/customer-a.yaml --profile customer-a-readonly --table Phoenix-Codex-Local-Spot-customer-a-State --state-region ap-south-2
```

必须审查并保留：

1. 账户/Region 身份与客户授权边界；B300/8 GPU/Spot supported usage class。unsupported 和读取失败不是“缺货”，不得换机型或 On-Demand。
2. 标准 AZ offerings；子网 AZ/VPC/available/address 证据及 SG VPC 一致性。offerings 表示类型可在该 AZ 提供，不表示当前 Spot 库存。
3. **P Spot vCPU quota**、元数据推导的 required vCPUs、已知 consumption 和 unknown 部分分别报告。总 quota 不是空闲 headroom；fulfilled request 与运行实例不能双算。服务端 quota rejection 不应误记为容量短缺。
4. 唯一有效 On-Demand 价格及 resolved effective ceiling。SPS、Spot price snapshot 仅 advisory，低/缺失 SPS 不能证明 B300 不支持 Spot。
5. 固定正整数版本的审核 LT（HYD 配置拒绝 `$Latest`、`$Default` 和 0）：B300 兼容 AMI/架构、NVIDIA 驱动与 CUDA、bootstrap 内容/hash、IMDSv2、加密磁盘、专用实例角色、instance/volume ownership tags。实际 `nvidia-smi`、业务 bootstrap 就绪只能靠后续授权实机验证。
6. owned Fleet、所有非 terminated 实例、saved FleetId 和 unresolved creation intent。重复 Fleet、残余机器、缺失可见性或不明请求都阻止新 full-target 创建。

Launch Template 的 **instance 和 volume** 都须带以下标签（只给 Launch Template 本身打标签不够）：

| 标签 | 客户 A 的值 |
|---|---|
| `Name` | `Phoenix-Codex-Local-Spot-customer-a-hyd-b300` |
| `managed-by` | `h100-spot-capacity-controller`（兼容历史标识） |
| `capacity-target-id` | `customer-a-hyd-b300` |
| `customer-id` | `customer-a` |
| `bootstrap-contract-version` | `standalone-b300-v1`（须与配置一致） |

上表对应本手册 target_id；若保留示例的 `-independent` 后缀，Name 与 capacity-target-id 也必须同步。客户 B 替换对应身份。`iam_instance_profile_arn` 填 **instance-profile ARN**，部署 `LaunchInstanceRoleArns` 填该 profile 内的 **role ARN**，二者不能混填。AMI 须兼容 p6-b300 的 x86_64 / Blackwell Ultra 软件栈，参考 [AWS P6 DLAMI 支持要求](https://docs.aws.amazon.com/dlami/latest/devguide/p6-support-dlami.html)；不要照搬 G6e 验证 AMI、驱动或 30 GiB 验证盘配置。

任何 mandatory prerequisite 未确认都停止初次写入；通过只读检查仍不保证分配。不要删除持久 intent、修改 token 或手动创建第二个 Fleet 来绕过阻塞。账户还须具备 EC2 Fleet 所需的 service-linked role（通常为 `AWSServiceRoleForEC2Fleet`）；如缺失，由客户管理员单独审核准备，不向 controller 增加无限制 IAM 创建权限。SNS 邮件/系统订阅由客户准备并确认，stack 只创建 Topic，不自动订阅个人地址。

未决创建最多在一小时内重放同一个冻结请求，超过窗口返回 `CREATION_REPLAY_WINDOW_EXPIRED` 并保留 intent，需操作员核实；不会换 token 继续盲建。`writer_busy` 表示当前有写者，`orphaned_capacity` 表示残留/总量异常，均不是无库存。日志 `capacity_write_attempted` 记录容量 API 是否尝试；`aws_write:null` 表示请求结果不明，不等于没有创建。

## 5. 待单独授权：控制平面部署（本次未执行）

CDK app 读取 `customer_id`、`target_id`、`region` context。客户 namespace 默认 Region 为 ap-south-2；文档显式传 Region，不依赖 operator 默认配置。AMI、网络、LT 和实例角色是既有审核输入，不由这个 stack 创建。以下 `--parameters` 都是实际 CfnParameter；必须替换为**精确审核 ARN**，不要 `*`。

```sh
# 只作本地模板审查：指定临时 outdir，命令结束自动清理
SYNTH_DIR=$(mktemp -d)
trap 'test -n "$SYNTH_DIR" && rm -rf -- "$SYNTH_DIR"' EXIT
cdk synth --app '.venv/bin/python app.py' --output "$SYNTH_DIR" \
  -c customer_id=customer-a -c target_id=customer-a-hyd-b300 -c region=ap-south-2
```

AWS diff/bootstrap/deploy 有账户读写或成本边界：先审核 IAM、replacement、retention、Lambda asset 和费用，再由获授权 operator 执行，**不要将以下示例当作已部署证据**：

```sh
cdk diff Phoenix-Codex-Local-Spot-customer-a-Controller --app '.venv/bin/python app.py' \
  --profile customer-a-operator -c customer_id=customer-a -c target_id=customer-a-hyd-b300 -c region=ap-south-2
cdk deploy Phoenix-Codex-Local-Spot-customer-a-Controller --app '.venv/bin/python app.py' \
  --profile customer-a-operator -c customer_id=customer-a -c target_id=customer-a-hyd-b300 -c region=ap-south-2 \
  --parameters TargetId=customer-a-hyd-b300 \
  --parameters LaunchTemplateArns=arn:aws:ec2:ap-south-2:ACCOUNT_ID:launch-template/REVIEWED_LT_ID \
  --parameters LaunchInstanceRoleArns=arn:aws:iam::ACCOUNT_ID:role/REVIEWED_INSTANCE_ROLE
```

B 使用 `customer-b` / `customer-b-hyd-b300` 及自己审核 ARN/profile，绝不复用 A 的状态表、target 或 IAM 角色授权边界。CDK deploy 只部署控制平面，target 尚未持久化或启用。DynamoDB、日志和 SNS 使用 RETAIN；stack destroy 不等于取消 Fleet 或删除状态。

## 6. 待单独授权：存入 disabled target，再显式 enable

```sh
# AWS DynamoDB 写；先检查 A 配置 enabled:false，得到持久化授权才执行
.venv/bin/python -m h100_spot_controller.cli target-put privateinputs/customer-a.yaml \
  --table Phoenix-Codex-Local-Spot-customer-a-State --state-region ap-south-2 --profile customer-a-operator --execute
.venv/bin/python -m h100_spot_controller.cli target-review customer-a-hyd-b300 \
  --table Phoenix-Codex-Local-Spot-customer-a-State --state-region ap-south-2 --profile customer-a-readonly
```

在 dry-run/所有前置证据已审核且获**容量花费授权**后，将 A 文件 `enabled` 改为 true，重新 local validate/dry-run，从最新 target-review 取得 `configuration_version`，替换下方 `REVIEWED_VERSION`：

```sh
.venv/bin/python -m h100_spot_controller.cli target-put privateinputs/customer-a.yaml \
  --table Phoenix-Codex-Local-Spot-customer-a-State --state-region ap-south-2 --profile customer-a-operator \
  --expected-version REVIEWED_VERSION --execute --enable-capacity
```

条件写失败需重新 review，不能盲覆盖。所有 approved AZ 始终共享一个 desired=1 maintain 请求。观察 capacity-review、CloudWatch MachineShortfall/RetryCount/InterruptionCount、SNS、Fleet errors/history 与 saved FleetId；持续短缺不触发另一个 Region 或新的 target-sized Fleet。将 quota、权限、无效 LT、throttling、未决写和 capacity waiting 区分处理。保留证据，不能把第一次见到机器当成重复补位已验证。

## 7. 安全暂停、取消需求、终止机器是三件事

1. **暂停 controller**：先取得配置写授权，把本地配置改回 `enabled:false`，target-review 取得最新 version，再 `target-put ... --expected-version REVIEWED_VERSION --execute`（不带 --enable-capacity）。暂停调度/禁用 target **不取消 AWS maintain 需求**，已有 Fleet 仍可能补位/计费。等待 in-flight writer 完成，并检查 pending intent 和 Fleet identity；不要通过删状态来“暂停”。
2. **取消 demand，保留现有实例**：确认 disabled 状态、resolved intent、全部 owned Fleet/instance 清单及正确客户/Region。先执行只读 cleanup preview，获得 Fleet 取消授权后才加 --execute。取消后运行实例仍计费。
3. **终止实例**：另一项不可逆授权；再次审查完整 owned instances，执行 --terminate-instances。不会隐式恢复工作负载。随后只读确认所有实例 terminal、Fleet terminal 且 intent 已有权威结果。

```sh
# 预览，不写；要求本地文件 enabled:false
.venv/bin/python -m h100_spot_controller.cli cleanup privateinputs/customer-a.yaml \
  --table Phoenix-Codex-Local-Spot-customer-a-State --state-region ap-south-2 --profile customer-a-readonly
# 单独获授权：取消 maintain Fleet，不终止已启动机器
.venv/bin/python -m h100_spot_controller.cli cleanup privateinputs/customer-a.yaml \
  --table Phoenix-Codex-Local-Spot-customer-a-State --state-region ap-south-2 --profile customer-a-operator --execute
# 另外获终止授权：取消 Fleet 并终止所有该 target/customer 的 owned 实例
.venv/bin/python -m h100_spot_controller.cli cleanup privateinputs/customer-a.yaml \
  --table Phoenix-Codex-Local-Spot-customer-a-State --state-region ap-south-2 --profile customer-a-operator --execute --terminate-instances
```

如 Fleet 暂不可见或 creation intent 未决，**不要**把空 preview 当成功取消、删除 intent、重启新 generation 或销毁 stack。先由 operator 按 saved identity、tag inventory、Fleet history 和服务端结果定位；`cleanup` 不是未知创建的自动恢复工具。最终记录 FleetId、取消结果、实例 terminal 证据、仍保留的状态/日志/费用项。参见 [通用运维手册](<operator-runbook.md>) 与 [部署前检查清单](<deployment-preflight-checklist.md>)，其中通用跨 Region/EKS 路径不适用于 HYD。
