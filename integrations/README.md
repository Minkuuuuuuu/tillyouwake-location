# TillyouWake 接入补丁

这些补丁依赖已有 Hermes / TillyouWake，不包含整个后端。先在独立副本验证，再人工合并到线上；不要把路径对应的整份文件覆盖回历史版本。

## 文件与基线

| 文件 | 基线 / 目标 |
|---|---|
| `main-gateway.patch` | 主仓库 `91521ab` → `90eb11b`，仅 gateway/proactive_cc.py、gateway/run.py、tests/gateway/test_proactive_cc.py；含初次定位和最终开关锁保护 |
| `app-voice.patch` | 2026-10-03 定位部署前的五个源码文件；路径以系统根目录相对路径表示，基线 LF 散列见 `source-manifest.json` |
| `frontend.patch` | 定位前端的部署前副本 → 2026-10-06 部署（开关、`[位置]` 预览、常用地点）；chat-models.js/.css 全量增量，index.html 只改两个资源版本号 |
| `relogin-frontend.patch` | 2026-10-06 部署后的前端 → 2026-10-07 App 内重新登录 Apple：chat-models.js/.css 增量，index.html 只改两个资源版本号 |
| `proactive-rounds.patch` | 聊天页显示祂主动开启的回合：chat-history.js（SSE + 按时间插入）与 voice_service.py（`/events`）；index.html 需另把 chat-history.js 的 `?v=` 改新以刷新缓存 |
| `app/plugins/tillyouwake_location/` | 新插件两文件，复制到 `/srv/tillyouwake-app-hermes/plugins/` |
| `app/plugins/tillyouwake_app/location_control.py`、`location_relogin.py` | 放入既有 App 插件包；依赖该包的 `model_settings.SettingsError`；重登子进程用 `runuser` 以采集器用户运行 `/opt/tillyouwake-location/collector.py --login-pipe` |

`app-voice.patch` 包含 App 独立源码的 `agent/turn_context.py`、App 插件 adapter/model_settings 和 voice 两文件。它选取原始定位增量，已去掉生产 config.yaml 和后续无关通话改动；补丁头规范化为 a/… 与 b/…。生产文件可能混用行尾，先在副本检查，勿整文件重写。

## 应用顺序

1. 安装采集器、systemd unit 和空白配置，保持关闭；参考上级 SETUP。
2. 在主仓库对应基线上检查并合入 `main-gateway.patch`。
3. 放置新插件 / location_control.py，再检查并合入 `app-voice.patch` 到对应 App/voice 独立副本。
4. 在 App 插件配置中启用 `tillyouwake_location`，遵循目标 Hermes 已有的插件配置格式。本仓库不包含生产 config.yaml，不要重写现有插件列表。
5. 检查并合入 `frontend.patch`；其前端使用既有模型设置桥与认证，不另增服务。
6. 检查并合入 `relogin-frontend.patch`（需先有 `frontend.patch`），并部署同版本 collector.py 与两个 App 插件文件后重启 App。
7. 检查并合入 `proactive-rounds.patch`，在无通话、无进行中回合时重启 voice；前端为静态文件。
8. 适当安排相关服务加载修改，完成本人账号配置与坐标校准后，由本人开启 App 定位。

三个补丁均可在匹配基线的临时目录用 `git apply --check <patch>` 检查；实际生产目录较新的 reset、主动通话等功能必须保留。这里没有一键执行生产变更的安装脚本。

## 测试与行为

仓库根目录的 unittest discover 覆盖采集/登录与 App 插件/开关。移动调度测试随主网关补丁提供，需要原 Hermes 测试依赖及隔离的状态路径；不要直接跑会写真实聊天状态的全量 suite。

位置在 `pre_llm_call` 的本轮 user context 中追加，仅 App text lane；事件通过原 voice proactive-message 进入同一 App 文字回合。原有主动联系总开关、app_text 出口、会话、忙碌与退避条件仍有效。移动事件最终交付与 App 开关使用同一个 `.config.lock`。

前端定位快照返回 `enabled/running/status/last_error/preview/places/mark_blocker`：`preview` 是插件同一格式器的 `[位置]` 原文（关闭时为 null），`places` 只有 id、名称、半径，不含中心；不返回账号、Key、设备 ID。POST 只接受 `{"enabled": bool}` 或 `{"place": {"action": "mark"|"rename"|"delete", …}}`，标记时后端自行取缓存当前样本，前端不能提交坐标。开启和关闭转换都会增加 generation，启动失败回滚还会再次增加；不要在自己新增的控制入口中省略这些语义。

重新登录 Apple：`location.relogin` 返回 `needed/busy/allowed/account`（账号仅脱敏）。POST `{"relogin": {"action": "start", "password": …}}` 返回 `relogin_flow.id`（32 位十六进制，仅这一次返回），之后 `{"action": "status"|"cancel", "flow": id}`、`{"action": "code", "flow": id, "code": "6 位数字"}`；`relogin_flow` 只含 `stage/kind/seconds_left`。同一时间一个流程，采集器运行中拒绝开始，5 分钟到期、取消或结束都会关闭管道并结束子进程组；密码与验证码只经子进程 stdin，不进 argv/env、文件、日志或响应。成功后不改配置与 generation，开关已开时只 `systemctl start --no-block`。
