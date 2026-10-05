# 部署与账号接入

运行环境：Linux / systemd、Python 3.12。这里说明全新安装；已经正常运行的部署不要重跑初始化、覆盖配置或清空会话。

## 1. 安装采集器

以 root 在确认过的目标服务器执行。先创建专用系统用户 `tillyouwake-location`，home 为 `/var/lib/tillyouwake-location`；建立代码目录 `/opt/tillyouwake-location`、配置目录 `/etc/tillyouwake-location` 和该状态目录。

- 将 `collector.py`、`setup-login.py` 放到代码目录，由 root 管理。
- 创建 `/opt/tillyouwake-location/.venv`，用仓库 `requirements.txt` 安装依赖。
- 将 `config.example.json` 复制为配置目录中的 `config.json`：root 所有、组为 `tillyouwake-location`，权限 `0640`；配置目录 `0750`。
- 将 `credentials.example.env` 复制为 `credentials.env`：root 所有、权限 `0600`。这是 systemd EnvironmentFile，**不能 shell source**；初次交互工具会写入真正的值。
- 状态目录归采集用户所有，权限 `0750`；`cookies/` 归采集用户所有、权限 `0700`。
- 创建配置目录中的 `.config.lock`，供 root 的 App 控制与主网关共享文件锁。
- 把仓库中的 unit 安装到 `/etc/systemd/system/tillyouwake-location.service` 并 `daemon-reload`。初始配置保持关闭，先不要启动采集。

若仅独立使用采集器，可用 systemd 管理启停；若接入 TillyouWake App，按 [integrations/README.md](integrations/README.md) 合入设置、注入及移动事件补丁，然后通过 App 开关管理。开机启用 unit 也不绕过代码的 enabled / 配置完整性检查。

## 2. 初始中国区账号接入

`setup-login.py` 是为已经确认的中国区账号编写的一次性工具，要求 root、交互终端、`enabled=false` 且尚未设置 device_id；它不是已配置系统的通用重登入口。

```bash
python3 /opt/tillyouwake-location/setup-login.py
```

本人在终端输入 Apple 账号、高德 Web 服务 Key、密码，并处理 Apple 的验证。密码不保存；可信会话会保存在受限 cookies 目录。Apple / 网络拒绝后停止，不连续重试。

如果上次密码 / 验证码已经成功，只有之后的会话登录或设备列表失败，可在本人在场且尚未配置设备时尝试一次：

```bash
python3 /opt/tillyouwake-location/setup-login.py --resume
```

这个模式只使用已有 session_token / trust_token，不自动退回密码或发验证码；先完成设备列表，再选择完整设备 ID，最后才询问高德 Key。中国区进程内边缘路由与限制见 README。模板的 icloud_edge 留空，正常模式沿用配置中的路由；只有将其设为 global 或使用 --resume 才开启该进程内路由。失败后需诊断阶段再决定是否适用续接。

设备 ID 包括全部末尾字符，不能丢掉 `=`。工具目前只校验格式，选择前应核对实际设备列表。成功配置后仍关闭，`source_crs` 留空；开启前必须校准。

## 3. 坐标系校准

在本人同意的一次真实采样中，将原始点与明确地标核对，区分 WGS-84 和 GCJ-02，不能凭“都是经纬度”直接选。`collector.py --probe` 会做真实 Apple 请求；不是日常健康检查，也不启动常驻采集。

采集器凭据由 systemd 提供；手动 probe 需通过受控进程环境载入账号，并以专用用户 / 正确 HOME 调用，不能直接 shell source 凭据文件或把 token 贴到命令行。不要把测试得到的坐标提交到本仓库。

确认后把配置 `source_crs` 设为 `wgs84` 或 `gcj02`。WGS-84 会通过高德转换后逆地理；给模型和移动计算的仍是标注了原坐标系的原始点。高德参数顺序为经度、纬度。

## 4. 开关与诊断

- App 状态：`GET /voice-api/v1/model-settings` 的 `location` 字段。
- App 开关：`POST /voice-api/v1/model-settings/location`，仅接受 `{"enabled": true/false}`，沿既有认证与 Origin 检查。
- 开启缺配置会拒绝；配置齐全不代表令牌有效。高德 Key 缺失时采集可只有坐标、无地址。
- 关闭先写 false / 新 generation，再请求停止 unit；不一定同步等进程退出。采集发布和移动交付都重查状态，拒绝过代数据。
- `status=ok` 的界面文案为“已开启，正在更新位置”；面板没有定时更新，需刷新状态或重开。
- `needs_login` 时先保留配置与 cookies，再诊断账号会话；采集器不会自动进行密码/2FA重登。尚无已验收的保留配置一键恢复命令。
- 启动时网络失败也会正常退出，避免自动认证循环；应先确认连通性，再由本人决定恢复采集。

日常读 systemd 的 ActiveState / Result / NRestarts / MemoryCurrent，和 latest 的 status、匹配关系、sampled_at / checked_at 年龄、精度、地址是否存在。不要为诊断读取或打印完整凭据、cookies、原始认证响应与位置记录。

## 5. 验证与回滚

本仓库离线测试命令见 README；真实 Apple 登录、位置请求和模型消息须另外安排。接入补丁先在独立副本 `git apply --check`，记录目标文件散列与备份。若目标已有其他改动，人工合并，不能强制覆盖整份 App / voice 文件。

采集器源码、App 插件、主网关补丁、前端补丁分别回滚。会话、配置和开关不是源码备份的一部分；回滚时保留本人当前开关选择和后续账号设置。
