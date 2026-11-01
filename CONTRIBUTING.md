# 贡献指南

感谢你愿意改进这个项目！

## 开发环境

```bash
pip install -r requirements.txt
pip install pyinstaller   # 只有打包时才需要
python app.py
```

- **Python 3.9+**（开发用 3.14）
- **Windows 10/11**（用到了 Win32 API 与注册表，Mac/Linux 跑不了）
- `requirements.txt` 里只列了运行时依赖 `pywebview`

## 代码结构

```
app.py          后端。分三块：
                  1) Win32 窗口控制（_hwnd / _set_pos / _drag_loop ...）
                  2) winget 与注册表（_run_winget / _scan_registry_meta ...）
                  3) pywebview API 类（class Api，前端通过 api.xxx 调用）

web/index.html  界面结构（纯静态 DOM）
web/style.css   样式，主题色用 CSS 变量，JS 运行时注入
web/app.js      交互逻辑，IIFE 包裹，不暴露全局
```

### 前后端通信约定

前端一律通过 `window.pywebview.api.<方法名>()` 调后端，**全部是 async/await**：

```js
const apps = await api.get_apps();
```

后端 `Api` 类的方法**必须返回可 JSON 序列化的值**（dict / list / 标量），不能返回 PyInstaller 打包后无法跨进程的对象。

### 几个别踩的坑

- **pywebview 就绪事件是小写 `pywebviewready`**。驼峰 `pyWebViewReady` 永远不触发 → 全站按钮失灵。
- **`[hidden] { display: none !important; }` 必须写**。不然 `.logpanel { display: flex }` 会盖过 `hidden` 属性，导致 ✕ 关不掉。
- **主题类要挂在 `documentElement` 而不是 `body`**。挂 body 时 html 的 `background: var(--bg)` 永远取 `:root` 的深色值，浅色模式下根画布仍是黑的。
- **`SetWindowPos` 声明了 argtypes 后，宽高不能传 `None`**。会抛 `TypeError` 被 except 静默吞掉 → 拖动失效但缩放正常。移动时传 `0` 表示不变。
- **pywebview 6.x 没有 `start_drag()`**。无边框窗口的拖动和 8 向缩放是用 Win32 自己实现的，见 `_drag_loop`。
- **无边框会清掉 `WS_THICKFRAME`** → 系统缩放热区也没了，所以在 HTML 里自己画了 8 个 `.rz` 手柄，别删。
- **日期格式要归一化**。注册表 `InstallDate` 有 `20261011` 和 Unix 时间戳两种，统一转成 Unix 秒再排序，否则日期排序会错乱。

## 提交前自查

- [ ] `python app.py` 能正常启动
- [ ] 改动涉及后端时，Windows 10 和 Windows 11 都试过
- [ ] 新增的用户配置项记得在 `load_settings()` 的默认值里给上
- [ ] 不要把 `settings.json` / `backup_index.json` 等本地数据提交上来（`.gitignore` 已处理）
- [ ] README 里的功能清单如果受影响，同步更新

## 提交信息

用中文或英文都行，格式参考：

```
feat: 增加按安装时间排序和筛选
fix: 修复窗口位置在多显示器下丢失
docs: 补充 winget 下载目录 yaml 说明
```

## 打包验证

改完后确认能正常打包：

```bash
build.bat
```

产物 `dist/SoftwareManager.exe` 应该约 22 MB，双击不弹黑框。
