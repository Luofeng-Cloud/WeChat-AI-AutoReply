<div align="center">

# 🤖 WeChat-AI-AutoReply
### 微信 AI 自动回复 (Zero-Mouse & Off-Screen Edition)

**基于视觉感知与 Win32 无痕挂载的 PC 微信 AI 自动回复**  
*100% 物理鼠标 0 移动 · 虚拟离屏隐形挂载 · 毫秒级视觉双轨感知 · 白名单硬核物理防火墙*

<p align="center">
  <a href="https://github.com/Luofeng-Cloud/WeChat-AI-AutoReply/releases/latest">
    <img src="https://img.shields.io/github/v/release/Luofeng-Cloud/WeChat-AI-AutoReply?color=brightgreen&label=Release&style=flat-square" alt="Release">
  </a>
  <img src="https://img.shields.io/badge/Platform-Windows%2010%20%7C%2011-0078D6?logo=windows&style=flat-square" alt="Platform">
  <img src="https://img.shields.io/badge/WeChat-3.x%20%7C%204.x%20Qt-07C160?logo=wechat&style=flat-square" alt="WeChat">
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&style=flat-square" alt="Python">
  <a href="LICENSE">
    <img src="https://img.shields.io/github/license/Luofeng-Cloud/WeChat-AI-AutoReply?color=orange&style=flat-square" alt="License">
  </a>
</p>

---

### 📦 Windows 用户一键体验入口

[![Download Release](https://img.shields.io/badge/🚀%20一键下载-Windows%20正式安装包%20(v1.0.1)-07C160?style=for-the-badge&logo=windows&logoColor=white)](https://github.com/Luofeng-Cloud/WeChat-AI-AutoReply/releases/latest/download/WeChat-AI-AutoReply_Setup.exe)
*(无需配置 Python 环境，下载 EXE 安装包双击即可秒级开启！)*

</div>

---

## 🌟 核心技术与产品亮点

- **🖱️ 100% 物理鼠标 0 移动**  
  摒弃传统自动化工具（PyAutoGUI、按键精灵）抢夺光标的落后方式，采用纯底层的 Win32 消息句柄投递（PostMessage / SendMessage）。在后台极速切入、粘贴、发送消息，**您的鼠标指针完全不移动、键盘打字不被打断**！

- **👻 虚拟离屏隐形挂载（Off-Screen Mode）**  
  微信窗口可一键收纳进 Windows 虚拟离屏空间（坐标负向离屏区域），桌面 0 弹窗、无视觉干扰。即便微信被完全遮挡或离屏隐身，DWM / PrintWindow 依然能高保真捕捉画面并静默秒回。

- **👁️ 毫秒级双轨视觉感知**  
  - **几何与像素级感知**：动态咬合微信侧边栏与未读红点，自适应 100% ~ 250% 任意 DPI 缩放及窗口自由拉伸。
  - **RapidOCR 本地高频引擎**：纯本地 ONNX Runtime 毫秒级文字切片识别，新消息 0.2 秒极速捕获，支持连发聚合应答。

- **🛡️ 白名单物理级硬核防火墙**  
  - 严格仅对**白名单内的指定好友**触发回复；
  - 物理级阻断任何微信群聊、服务号、订阅号、陌生人私聊；
  - 配备发送前双重二次校验与重定向熔断机制，杜绝任何串发事故。

- **🎭 千人千面个性化人设（Personas）**  
  支持针对不同好友定制专属语气模型：死党互怼、商务伙伴礼貌回应、老友温馨问候等，无缝贴合主人本人口吻。

- **🌐 主流大模型无缝兼容**  
  原生支持 OpenAI 标准协议，可接入 **DeepSeek (V3/R1)**、**MiniMax**、**Qwen (通义千问)**、**Moonshot (Kimi)**、**智谱 GLM**、**ChatGPT (GPT-4o)** 等主流 API。

---

## 📊 架构机制对比

| 对比维度 | 传统 Hook / 内存注入 | 传统按键精灵 / 坐标点击 | 🤖 本项目 (视觉离屏+消息调度) |
| :--- | :--- | :--- | :--- |
| **安全性** | ❌ 易被特征风控/封号 | ⚠️ 易点击漂移 | ✅ **100% 外部感知无注入，零封号风险** |
| **鼠标干扰** | ✅ 不影响鼠标 | ❌ **严重抢夺光标，无法正常工作** | ✅ **100% 物理鼠标 0 移动，零干扰** |
| **窗口状态** | 必须在前台或特定状态 | ❌ 窗口必须顶层置顶 | ✅ **离屏挂载/后台静默/被覆盖均可秒回** |
| **防串发机制** | 依赖内存协议解析 | ❌ 极易错位串发 | ✅ **OCR + 句柄 + 白名单三重物理熔断** |
| **版本适应性** | 每次微信更新必须重写基址 | 依赖固定分辨率 | ✅ **完美通配微信 3.x / 4.x Qt 架构** |

---

## 🚀 极简上手指南

### 方式一：下载 Windows 一键安装包（推荐普通用户）

1. 从 [Releases 页面](https://github.com/Luofeng-Cloud/WeChat-AI-AutoReply/releases/latest) 下载 微信AI自动回复_Setup.exe；
2. 双击安装后运行桌面上的【微信AI自动回复】控制中心；
3. 配置您的 API Key、将好友昵称加入白名单，点击 **【🚀 启动 AI 自动回复】** 即可！
4. 可选：点击 **【👻 隐藏微信至离屏】**，微信将立刻无缝隐身，后台静默为您代答。

### 方式二：源码运行与二次开发（开发者）

#### 1. 克隆仓库
```bash
git clone https://github.com/Luofeng-Cloud/WeChat-AI-AutoReply.git
cd WeChat-AI-AutoReply
```

#### 2. 安装依赖
```bash
pip install -r requirements.txt
```

#### 3. 配置文件准备
复制配置模板文件：
```bash
copy wechat_config.example.json wechat_config_dev.json
```
在 `wechat_config_dev.json` 中配置您的 `api_key`、`api_base` 以及好友白名单 `whitelist`。

#### 4. 运行可视化控制中心
```bash
python wechat_bot_gui_dev.py
```
或直接无界面静默守护运行核心：
```bash
python wechat_ai_bot_dev.py
```

---

## ⚙️ 核心配置说明 (`wechat_config.json`)

```json
{
    "ai_engine": "openai_api",
    "openai_api": {
        "api_key": "sk-your-key-here",
        "api_base": "https://api.xiaomimimo.com/v1",
        "model": "mimo-v2.6-flash",
        "temperature": 0.7
    },
    "whitelist_mode": true,
    "whitelist": [
        "好友微信备注名A",
        "好友微信备注名B"
    ],
    "friend_personas": {
        "好友微信备注名A": "死党互损模式：说话幽默接地气、互怼开玩笑，绝不客套拘谨",
        "默认": "真人好友模式：语气随和、口语化、真诚得体"
    },
    "check_interval_seconds": 0.5
}
```

---

## 📁 仓库结构

```text
WeChat-AI-AutoReply/
├── wechat_ai_bot_dev.py          # 后台常驻感知与调度核心 (OCR+Win32消息路由+离屏抓图)
├── wechat_bot_gui_dev.py         # 现代化控制中心 GUI (Tkinter + 实时日志 + 托盘控制)
├── kill_wechat_bot_dev.py        # 安全进程清理终结脚本 (精准定向关闭守护)
├── wechat_config.example.json    # 工业级通用脱敏配置文件模板
├── .env.example                  # 环境变量配置模板
├── requirements.txt              # Python 依赖清单
├── .gitignore                    # 工业级排除规则 (严防私密配置与日志外泄)
├── LICENSE                       # MIT 开源许可证
└── README.md                     # 项目说明文档
```

---

## ⚠️ 免责声明 (Disclaimer)

1. 本项目仅供技术交流与计算机视觉/自动化技术学习研究使用；
2. 本项目不涉及任何反编译、DLL 注入或针对微信协议的破解篡改，全流程遵循 Windows 标准窗口消息与辅助感知交互规范；
3. 请合理配置并使用自动化助手，使用者须自行承担使用过程中的相关法律与合规责任。

---

## 📄 开源许可证 (License)

本项目基于 [MIT License](LICENSE) 许可协议开源。