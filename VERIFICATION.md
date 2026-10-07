# 上传前验证（2026-10-05）

此次上传的是源码和 TillyouWake 接入补丁，没有真实账号、设备 ID、Key、cookie 或位置记录；没有改变生产开关、重启业务服务或发模型消息。

| 检查 | 结果 |
|---|---|
| `python3 -m unittest discover -s tests -p 'test_*.py' -v` | Linux 临时源码目录，27 项通过；Apple / AMap 为假会话、内存运输或测试数据 |
| `python3 setup-login.py --self-check` | 凭据编码与原子配置自检通过，使用临时目录 |
| `git apply --check integrations/main-gateway.patch` | `91521ab` 的三个主仓库源码文件副本，通过 |
| `git apply --check integrations/app-voice.patch` | 2026-10-03 五个源码基线副本（LF 规范化），通过 |
| `git apply --check integrations/frontend.patch` | 定位前端三个部署前文件副本，通过 |
| 源码筛选 | 仅明确源码清单；不使用生产 config.yaml、不打包环境/数据库/日志/备份 |
| 数据检查 | 未检出已知个人账号、VPS 地址/SSH Key 路径或实际校准地标；凭据示例为空 |

采集器、交互工具、位置插件、开关模块与部署源码一致；测试调整仓库相对路径，原 `turn_context` 参数断言改读对应接入补丁，并固定注入测试时钟，防止夹具随日期过期。部署与登录说明已更新，避免沿用旧的 shell source 凭据和一键重登指引。

主网关 53 项及前端模拟 API 9 项是原部署的历史记录，本次只检查它们的接入补丁，不宣称重新跑完或完成所有真机验收。

# 同步验证（2026-10-06）

源码与生产当前部署逐文件一致（collector.py、setup-login.py、两个 App 插件文件；补丁应用后与线上 chat-models.js/.css、chat-history.js、voice_service.py 相同）。未改生产设置、未重启服务、未请求 Apple / 高德、未发模型消息。

| 检查 | 结果 |
|---|---|
| `python -m unittest discover -s tests -p 'test_*.py'`（采集器 venv） | 45 项通过：原 27 + 重登 6 + 常用地点 7 + 预览 5；系统 Python 未装 pyicloud 时相关用例会导入失败 |
| `setup-login.py --self-check` | 通过 |
| `git apply --check integrations/frontend.patch` | 2026-10-03 定位前端部署前三文件，通过；index.html 仅改资源版本号 |
| `git apply --check integrations/proactive-rounds.patch` | 基线 chat-history.js（2026-10-05）与 voice_service.py（2026-10-06 事件前），通过，结果与线上一致 |
| SSE 端点（部署前，本机临时端口） | 3 项：无 Access 403、变更即推 rounds、开着的流不阻塞停止 |
| 真实页面 + 模拟 API（headless Chromium） | 卡片预览 / 地点标记 / 关闭隐藏；空闲 25 秒无 /messages、隐藏无请求、回到前台补读且不重复 |

未验证：经 Cloudflare 隧道的真实 SSE、手机耗电、真机上地点标记与主动回复即时出现。`main-gateway.patch`、`app-voice.patch` 本次未变。

# App 内重新登录 Apple（2026-10-07）

源码与生产部署逐文件一致（collector.py、location_control.py、location_relogin.py；`relogin-frontend.patch` 应用到 10-06 部署副本后与线上 chat-models.js/.css、index.html 相同）。部署时未改开关（保持关闭）、未请求 Apple / 高德、未发模型消息。

| 检查 | 结果 |
|---|---|
| `python -m unittest discover -s tests -p 'test_*.py'`（采集器 venv） | 66 项通过：原 45 + 登录管道 10 + App 重登流程 11 |
| `setup-login.py --self-check` | 通过 |
| `git apply --check integrations/relogin-frontend.patch` | 2026-10-06 部署后的三个前端文件副本，通过 |
| 真实页面 + 模拟 API（headless Chromium） | 入口仅在需要时出现；脱敏账号按文本渲染；密码 / 验证码提交后立即清空，不在 DOM 与 local/sessionStorage；登录中锁定开关；成功后开关仍关；取消、网络失败、忙碌提示 |
| 线上（不触达 Apple） | App 重启后健康；快照 `relogin.needed=true`、开关关闭；以采集器用户真实拉起 `--login-pipe` 并给空密码，未登录即退出（cancelled）；登录锁可取得且拒绝第二个 |

未验证：真实 Apple 登录、验证码推送与核对、登录后 Find My 设备确认，需本人真机完成。

## 失败诊断日志（2026-10-07 13:03）

首次真机重登失败后，App 日志里看不到失败停在哪一步，所以补了一行安全日志：`location relogin end stage=… kind=… from=… seconds=… http=… at=… chain=…`。只有 Apple 的 HTTP 状态、请求路径（不含主机和查询参数）和异常类型名；不含报错原文、账号、密码、验证码、cookie 或响应内容。离线测试通过：登录管道 11 项、App 流程 12 项，另核对了脱敏和日志里不含密码；线上用空密码真实拉起子进程检查过（不触达 Apple）。
