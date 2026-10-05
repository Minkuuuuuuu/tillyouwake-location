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
