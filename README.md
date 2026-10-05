# TillyouWake Location

iCloud Find My 设备位置采集器，以及 TillyouWake / Hermes 的 App 位置上下文、移动唤醒和开关接入代码。

本仓库保存定位模块源码和接入补丁。采集器可以独立运行；App 开关、文字上下文和移动唤醒依赖已有的 TillyouWake App、voice 设置桥及 Hermes 网关，不是一套独立聊天 App。

## 功能与规则

- 约每 60 秒查询选定设备，原子写一份 `latest.json`，没有轨迹数据库。
- App 文字、聊天语音条以及 App 文字自唤醒，在每轮模型调用前读取本地最新位置；实时通话和通话中打字不追加位置。
- 距上一次成功移动唤醒的采样点至少 200 米时，额外开启普通 App 文字回合。距离是两点间 Haversine 距离，不是累计步行路程。
- 两次移动派发至少间隔 180 秒；等待时合并到最新位置。首次有效位置只建立基点；回合成功后更新基点，模型选择 `NO_REPLY` 也算成功。
- 移动样本需不超过 300 秒、误差不超过 100 米；普通上下文可展示最后已知位置或低精度位置，但明确注明限制。
- 关闭开关先使消费者停止读取，再请求停止采集；新开/关都会更换 generation，丢弃过代结果。已交给模型的回合和聊天历史不撤回。
- 每分钟采集不调用大模型；AMap 地址按位置距离和时间缓存。位置上下文与额外唤醒会有模型输入 / 回合成本。

## 文件

| 路径 | 用途 |
|---|---|
| `collector.py` | 可信会话启动、设备采集、坐标规范化、地址缓存 |
| `setup-login.py` | 本人交互的中国区初始登录 / 可信会话续接工具 |
| `systemd/tillyouwake-location.service` | 独立用户运行与受限状态目录 |
| `config.example.json`、`credentials.example.env` | 未启用、无账号或设备数据的模板 |
| `tests/` | 离线采集、登录运输、注入与开关检查 |
| `integrations/app/plugins/` | 位置插件与开关控制模块 |
| `integrations/*.patch` | 主网关、App/voice 和 PWA 的定位接入增量 |
| `integrations/README.md` | 补丁目标、基线与应用顺序 |

部署与登录见 [SETUP.md](SETUP.md)。代码基于 Python 3.12 / Linux，依赖锁定 `pyicloud==2.7.0`，另补 `rich<15`。登录使用了该库的私有方法，升级前需要重新验证。

## 登录与中国区连接

密码 / 验证码仅在本人终端交互输入，密码不保存；常驻只读取保存的可信会话，每次进程启动做一次 token accountLogin，并在同一实例读取 Find My。令牌失效不会退回密码或自动触发 2FA。

中国区可选 `icloud_edge=global`：仅在本进程中，把严格白名单 `(setup|p\d{1,3}-fmipweb)\.icloud\.com\.cn` 的 DNS 解析目标改为 `setup.icloud.com`；请求 URL、Host、SNI 和 TLS 校验保持原值。不要简单去掉每个分片域名的 `.cn`。该路由在原部署上成功，不保证所有网络 / 账号都适用。

高德使用 Web 服务 Key；没有 JS 地图，因此不需要 JS 安全密钥。原始坐标系必须先校准，再决定是否转换到 GCJ-02 逆地理。原始坐标与坐标系不能混用。

## 离线测试

在 Linux、仓库根目录，安装依赖后执行：

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 setup-login.py --self-check
```

测试使用假会话、内存运输或临时文件，不要求真实 Apple / 高德凭据。主网关的移动测试包含在 `integrations/main-gateway.patch` 中，需要对应 Hermes 基线和隔离的状态目录，不能直接对生产状态跑全量测试。

2026-10-05 上传前已在临时 Linux 源码目录完成 **27 项测试与 self-check**，三个补丁的基线检查均通过，详见 [VERIFICATION.md](VERIFICATION.md)。

## 验证范围与已知边界

2026-10-03 原部署已完成本人登录、设备选择、一次坐标校准，以及开启后的持续采集和地址转换。历史隔离记录：采集与登录 22 项、注入/开关 5 项、主网关相关 53 项、前端模拟 API 9 项。它们不是所有入口的完整真机验收；自然移动唤醒和实际聊天位置使用仍须逐项验证。2026-10-05 上传只整理源码和离线验证，不额外请求真实位置。

- 初始配置工具拒绝覆盖已配置设备，尚无通用的“保留配置一键重登”。会话过期先诊断，不要删 cookies 或循环输密码。
- 设备 ID 只做格式校验，需完整复制（包含末尾填充字符）；尚未改为设备列表成员校验。
- App 状态面板没有周期拉取，刷新状态或重开面板获取新状态；“已开启，正在更新位置”是正常 `ok` 文案。
- 地址不能保证确认楼栋、楼层或人在手机旁边；设备采样可能旧于本轮检查时间。

本仓库不包含真实账号、Key、cookie、位置快照或生产配置。设计参考：[Ariakitty/icloud-location-tracker](https://github.com/Ariakitty/icloud-location-tracker)；当前代码及中国区排错实现由本项目独立完成。
