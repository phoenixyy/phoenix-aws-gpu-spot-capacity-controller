# 场景：没有 GPU quota，先验证 CPU Spot 控制链路

**先看当前可用程度：** CPU profile、示例和本地校验可使用；公开仓库目前没有可直接用于任意账户的完整 CPU 云端部署方案。发布的打包器采用不可部署的离线账号占位符 `000000000000` 和固定示例区域，实际通过的 IAM 修改没有回写为通用发布模板。标准 app.py 又没有给 CPU 设置 VALIDATION_RUN_ID。不要把 B300/通用 GPU 的 deploy 命令套在 CPU 上。

本页区分「现在能照做」与「需先适配」。缺失步骤不会用伪命令掩盖。

## 1. 在本地检查 CPU 配置（现在能做）

完成 [安装](start-here.md) 后：

```sh
mkdir -p privateinputs
cp config/cpu-validation.example.yaml privateinputs/cpu.yaml
.venv/bin/python -m h100_spot_controller.cli validate-target privateinputs/cpu.yaml
```

**检查点：** valid=true、enabled=false、cpu-validation、c6i.large、desired/maximum=1。示例只有占位符，所以结构校验通过不表示可部署。

## 2. 准备只读输入（先取得账户授权）

管理员准备新的 run/target 专用 LT/profile/SG、实际 AMI、AZ 子网与 Standard Spot quota。边界：单区域、c6i.large、私网无 public IPv4、无 SG 入站、加密根盘、IMDSv2、无需联网下载的 bootstrap。每轮唯一 validation_run_id，ownership purpose=cpu-spot-validation、validation-run-id 匹配；LT instance/volume 标签与 target 一致。

先保持 enabled=false，填写资源输入。dry-run/preflight 要预览 Fleet 时需要明确审核的 start/expiry，最长 30 分钟。不要先填已经过期的历史时间；没有时间时失败不是库存不足。

```sh
export READ_PROFILE='REPLACE_READONLY_PROFILE'
.venv/bin/python -m h100_spot_controller.cli discover privateinputs/cpu.yaml --profile "$READ_PROFILE"
.venv/bin/python -m h100_spot_controller.cli preflight privateinputs/cpu.yaml --profile "$READ_PROFILE"
```

**检查点：** preflight.ready=true，Standard quota/2vCPU、网络、AMI、启动合同、价格通过；没有创建容量。cap<=0.10 美元/小时，review budget<=1 美元，这不是账单硬停。

## 3. 云端测试前的部署适配闸门（当前不能直接复制完成）

以下必须由维护者完成和审核，不能由使用者猜测：

- 把 CPU 专用部署中的原账户/区域约束适配到已批准的新环境，不取消隔离范围。
- 提供完整且审查过的运行 IAM；不要使用未发布的本机策略，也不要复制测试时放宽的条件当通用安全策略。
- Lambda 的 STATE_TABLE_NAME、TARGET_ID、VALIDATION_RUN_ID 必须绑定该轮唯一身份；标准 CDK app 没有第三项。
- 专用定时规则先 disabled；代码包、表、日志、LT/SG/profile 和删除清单独立。
- 在新环境离线回归、模板/IAM 检查后，单独批准部署和实际 30 分钟费用窗口。

如果你希望首次拿仓库就能立即进行 AWS 实验，**不要继续这条路径**；需要维护者先补齐通用 CPU 部署支持。现有 `scripts/build-cpu-test-package.py` 是离线打包器，不是部署/自动清理命令。

## 4. 适配完成后的实际验收顺序

只有第 3 步通过才进入：

1. 专用部署就绪，存入 disabled target；禁用调用不产生容量写。
2. 设置批准的实际 UTC start/expiry；不能沿用过期窗口。
3. 使用 target-review 的最新版本，按 `target-put --expected-version ... --execute --enable-capacity` 启用。STATE 中应保存 run/window、冻结请求/token。
4. 确认唯一 Fleet 和第一台运行实例；请求结果不明时不换 token。
5. 单独批准并主动终止一次确切 owned 实例；观测同一 Fleet 的另一 InstanceId。此步骤不在本页提供通用终止脚本，避免在未完成适配时误执行。
6. reconcile 返回 healthy，fulfilled=1；验证这是同一 Fleet，不是另一笔创建。
7. 按 [清理指南](stop-and-cleanup.md) 禁用且保留 run/start/expiry、取消需求、终止并确认归零；再删该轮控制平面、EBS 和暂存对象。

超时而未补位属于验收未完成；到期终止请求不证明资源已清干净。不得延长旧窗口或复活旧 run。

## 已验证什么

首尔的一次独立测试验证了持久创建、主动终止、同 Fleet 补位与清理，并修复 DescribeFleetInstances 无 paginator 的兼容问题。它不是任意账户可部署证明，也不是 B300 获配、AWS 实际回收警告、GPU readiness 或 checkpoint 恢复证明。实现合同详见 [CPU profile 说明](../cpu-validation.md)。
