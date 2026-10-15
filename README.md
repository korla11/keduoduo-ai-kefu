# 客多多AI客服系统 · 通用安装包（Windows / Mac / Linux）

本包是 VPS 上运行版本的完整拷贝（抖音来客、抖音私信、小红书等全部模块），并带上了账号 **13200090009** 的现有设置：
AI 回复设置（DeepSeek 接口）、黑名单、知识库/话术、二次触达与留资记录、仪表盘数据。

> ⚠️ **不含抖音来客的浏览器登录**，装好后需要在页面里重新扫码登录来客。

## 零、Windows 安装（Windows 10/11）

1. 解压 `kdd_package_universal.zip`，进入 `kdd_package` 文件夹，**双击 `install_windows.bat`**。
   - 没有 Python 3.10+ 时会自动用 winget 安装 Python 3.12；若失败，请到 https://www.python.org/downloads/ 安装（勾选 “Add python.exe to PATH”）后再双击一次。
   - 脚本会：安装到 `%USERPROFILE%\KeDuoDuo` → 建虚拟环境 → 安装依赖 → 下载 Chromium → 注册「任务计划程序」开机登录自动后台启动（无窗口，异常退出每分钟自动重启）→ 打开 http://localhost:4999
2. 用 **13200090009** 和原密码登录 → 「抖音来客」扫码登录 → 打开监控。
3. 启动/停止：双击 `%USERPROFILE%\KeDuoDuo\start.bat` / `stop.bat`。日志在 `KeDuoDuo\logs\gateway.log`。
4. Windows Defender 防火墙若弹窗，选「允许」（仅本机访问也可以拒绝公用网络）。

### 保持电脑常开（重要）
以管理员身份打开 PowerShell：
```powershell
powercfg /change standby-timeout-ac 0     # 插电永不睡眠
powercfg /change hibernate-timeout-ac 0   # 不休眠
powercfg /change monitor-timeout-ac 10    # 屏幕可以关，不影响运行
```
- 设置自动登录（任务在用户登录后才启动）：运行 `netplwiz`，取消勾选「要使用本计算机，用户必须输入用户名和密码」；
  Windows 11 若看不到此选项，先在 设置→账户→登录选项 关闭「仅允许 Windows Hello 登录」。
- BIOS/设置里可开启「来电自动开机」；Windows 更新请设置活跃时间，避免半夜自动重启后无人登录。
- 卸载：`stop.bat` → 任务计划程序删除 `KeDuoDuoGateway`（或 `schtasks /Delete /TN KeDuoDuoGateway /F`）→ 删除 KeDuoDuo 文件夹。

## 一、Mac 安装（推荐一台常开的 Mac）

1. 解压 `kdd_package.zip`，打开「终端」，进入解压后的文件夹：
   ```bash
   cd ~/Downloads/kdd_package
   bash install_mac.sh            # 默认安装到 ~/KeDuoDuo，也可写成 bash install_mac.sh /路径
   ```
   - 需要 Python 3.10+；没有的话脚本会用 Homebrew 安装 python@3.12（没装 Homebrew 先去 https://brew.sh 装）。
   - 脚本会：建虚拟环境 → 安装依赖 → 下载 Playwright Chromium → 注册开机自启（launchd，后台运行、崩溃自动重启）。
2. 浏览器打开 **http://localhost:4999**
3. 用 **13200090009** 和原来的密码登录（密码与 VPS 上一致）。
4. 进入「抖音来客」→ 扫码登录来客 → 打开监控。
   （安装包里监控默认是**关闭**的，必须手动打开，避免和 VPS 同时运行。）

### 保持 Mac 常开不休眠（重要）
- 系统设置 → 电池/节能：「显示器关闭时防止自动进入睡眠」打开；接电源。
- 或终端执行（永久，需管理员密码）：
  ```bash
  sudo pmset -a sleep 0 disksleep 0
  sudo pmset -a autorestart 1     # 断电来电后自动开机
  ```
- 临时方式：`caffeinate -dimsu &`
- launchd 用户服务需要该用户**已登录**；建议在「用户与群组」里开启自动登录。合盖会休眠，笔记本请外接电源并保持开盖。

### 常用命令
```bash
~/KeDuoDuo/start.sh     # 启动
~/KeDuoDuo/stop.sh      # 停止（停止后不会自动拉起，直到再次 start 或重启电脑）
tail -f ~/KeDuoDuo/logs/gateway.log                              # 网关日志
tail -f ~/KeDuoDuo/app/users/udacd8cbc4b/douyin_laike.log        # 来客回复日志
```
卸载：`~/KeDuoDuo/stop.sh; rm ~/Library/LaunchAgents/com.keduoduo.gateway.plist; rm -rf ~/KeDuoDuo`

> 也可以在任何系统直接运行通用入口：`python3 install.py install`（Mac/Linux 的 install_*.sh 和 Windows 的 .bat 都是调用它）。

## 二、Linux 安装（可选）
```bash
bash install_linux.sh ~/KeDuoDuo     # root 运行会注册 systemd 服务 keduoduo（开机自启）
```
非 root：用 `~/KeDuoDuo/start.sh` / `stop.sh`。

## 三、注意事项
- ❗ **不要让 VPS、Windows、Mac 中任意两台同时登录/监控同一个来客账号**：会重复回复、互相顶掉登录。切到 Mac 前先在 VPS 页面里关闭来客监控（或停掉 VPS 服务）。
- 默认只允许本机访问（127.0.0.1:4999）。要让同一局域网其他设备访问，把安装目录里 `kdd_settings.json` 的 `host` 改为 `0.0.0.0` 后执行 stop/start。
- 包内含 AI 接口密钥（DeepSeek API Key）和登录账号的密码哈希，请勿外传。
- 重新运行安装脚本可升级程序文件，**不会覆盖**已有的 users/ 数据和账号。
- 每个用户的后端由网关以子进程方式启动（替代 VPS 上的 systemd `biligo-user@<uid>`）；后端意外退出时网关会在 30 秒内自动拉起。
