# 微信AI自动回复 · Electron 控制中心

这是 [WeChat-AI-AutoReply](https://github.com/Luofeng-Cloud/WeChat-AI-AutoReply) 的 Electron 版前端。

后台核心没有用 JavaScript 重写，而是保留为 Python 引擎：视觉感知依赖 RapidOCR（ONNX Runtime），
无痕后台挂载依赖 Win32 消息与 PrintWindow/DWM。这两部分换成 JS 只能做出功能缩水的仿制品，
所以架构上选择「Electron 负责界面与进程监管，Python 负责感知与调度」。

```
┌──────────────────────────────────────────────┐
│  Electron 渲染层（8 个功能页，深色主题）        │
├──────────────────────────────────────────────┤
│  preload：contextBridge，13 个受控方法         │
├──────────────────────────────────────────────┤
│  主进程：进程监管 / 解释器探测 / 文件对话框      │
├──────────────────────────────────────────────┤
│  stdin/stdout 上的行分隔 JSON-RPC 2.0         │
├──────────────────────────────────────────────┤
│  Python 引擎（wechat_engine 包）              │
│    win32 · vision · ocr · ai · statemachine   │
└──────────────────────────────────────────────┘
```

## 为什么这样拆分

| 关注点 | 归属 | 原因 |
| :--- | :--- | :--- |
| 界面、路由、表单、实时预览 | Electron | 原生桌面体验，无需 Python GUI 依赖 |
| OCR 文字识别 | Python 引擎 | RapidOCR + ONNX Runtime，无可比的 JS 等价物 |
| 窗口抓取与消息投递 | Python 引擎 | ctypes 直连 user32/gdi32 |
| 配置读写 | Python 引擎 | 单一写入方，避免两端各写一份配置互相覆盖 |
| 进程生命周期、解释器探测 | 主进程 | 只有它能 spawn 与监督 |

引擎是配置的唯一真实来源：界面上的每次修改都会写回引擎，并**从回包里**刷新本地副本，
所以界面显示的内容不可能和磁盘上的文件不一致。

## 快速开始

```bash
cd electron-app
npm install

# 引擎依赖（若已有 Python 环境）
pip install -r requirements.txt

# 开发模式
npm run dev

# 构建 Windows 安装包
npm run build:win
```

首次启动时应用会自动探测可用的 Python 解释器。若缺少依赖，界面会给出提示并可以一键安装。

### 环境要求

- Windows 10 / 11（x64）
- Node.js 20+（仅开发与构建需要）
- Python 3.9+ 与 `requirements.txt` 中的依赖（或使用打包后的引擎 exe）
- 已登录的 PC 微信 3.x / 4.x

## 目录结构

```text
electron-app/
├── src/
│   ├── main/            # Electron 主进程
│   │   ├── index.ts     # 应用外壳、单实例锁、窗口生命周期
│   │   ├── engine.ts    # 引擎监管：一次性调用与常驻 RPC 双通道
│   │   ├── python-env.ts# 解释器探测（含注册表重建用户 PATH）
│   │   └── ipc.ts       # 受控 IPC 通道
│   ├── preload/         # contextBridge 暴露的唯一界面能力
│   ├── renderer/        # 界面：8 个页面 + 设计系统
│   └── shared/          # 前后端共用的协议类型定义
├── wechat_engine/       # Python 引擎包
│   ├── win32.py         # Windows API 封装（窗口、抓图、消息投递）
│   ├── vision.py        # 感知：红点、标题、气泡解析、发送验收
│   ├── naming.py        # 名称匹配与白名单/群聊防火墙
│   ├── layout.py        # 与窗口尺寸、DPI 无关的布局计算
│   ├── ai.py            # 大模型客户端与回复清洗
│   ├── statemachine.py  # 扫描 → 决策 → 发送 → 验收 主循环
│   ├── config.py        # 配置模式、迁移、原子持久化
│   ├── protocol.py      # JSON-RPC 传输层
│   └── service.py       # RPC 方法实现
├── tests/               # 203 个 Python 单元测试
├── scripts/smoke.mjs    # 驱动真实 Electron 的冒烟测试（含截图）
└── build/engine.spec    # 可选的 PyInstaller 引擎打包配置
```

## 界面

| 页面 | 作用 |
| :--- | :--- |
| **总览** | 微信窗口状态、守护进程指标、实时画面预览、当前识别到的会话与待回复内容 |
| **运行日志** | 引擎实时日志流，按级别与关键字过滤 |
| **AI 引擎** | API Base / Key / 模型，温度、超时、重试，内置服务商快速填充，配置检查与真实调用测试 |
| **人设与 Prompt** | 12 个内置人设，自由编辑，切换时双向保留用户自己的文本 |
| **白名单与防扰** | 白名单/黑名单管理，以及把微信里的昵称原样输入来验证能否匹配 |
| **好友人设** | 为特定好友叠加专属语气（1.x 引擎支持但从未有过入口的功能） |
| **回复沙盒** | 不接触微信地验证 Prompt 与模型效果，可查看实际发送的 system 提示词 |
| **设置与诊断** | 回复延迟、记忆轮数、屏蔽词、日志级别、配置导入导出、环境自检 |

## 测试

```bash
# Python 引擎单元测试（203 个）
npm run test:python

# 真实 Electron 冒烟测试：启动引擎、渲染全部页面、截图到 .smoke/
npm run smoke
```

冒烟测试会校验每个截图的标题与页面 ID 一致，因此产出的截图可以放心作为界面回归证据。

## 开发说明

- 请求在引擎侧用线程池并发处理，因此**同一批 RPC 请求之间没有顺序保证**。
  依赖前一个结果的调用必须分开发起。
- `stdout` 属于 RPC 传输层。引擎里任何打印都必须走日志总线（输出到 stderr），
  否则会污染协议流。
- 界面不直接操作文件系统：所有能力都在 `src/preload/index.ts` 中显式列出。

## 免责声明

本项目仅供技术交流与计算机视觉/自动化技术学习研究使用。不涉及反编译、DLL 注入或对微信协议的破解篡改，
全流程遵循 Windows 标准窗口消息与辅助感知交互规范。使用者须自行承担使用过程中的相关法律与合规责任。

## 许可证

MIT License
