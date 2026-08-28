# Cookies 导出指南

Playwright 需要你登录后的 session cookies 才能访问投注页面。这个文件记录了导出的方法——**仅需做一次**，除非登录过期。

## 方法一：Chrome/Edge DevTools（无需插件）

1. 在浏览器中**登录** `https://www.08a2zp.vip:9967`
2. 按 F12 打开 DevTools
3. 切换到 **Application** (或"应用") tab
4. 左侧侧边栏选择 **Cookies** → `https://www.08a2zp.vip:9967`
5. 你会看到一列表格 (Name/Value/Domain/Path 等)
6. 在表格上方的空白处**右键** → **选中第一条** → **右键** → 选择 "Show folder"

   > 更直接：全选 (Ctrl+A) → 右键 → **Copy** → **Copy as JSON**
   > 如果没有"Copy as JSON"选项，见下方的备用方法。

7. 打开记事本，粘贴，保存为 `cookies.json`

### 备用方法（DevTools 无 Export 功能时）

1. 在 Console tab 中粘贴以下代码并按回车：
   ```javascript
   copy(document.cookie)
   ```
   (这只复制了 httpOnly=False 的 cookie，可能不够)
   
   更好的方法：
   ```javascript
   let cookies = await chrome.cookies.getAll({url: 'https://www.08a2zp.vip:9967'});
   // 注意：在某些页面 chrome.cookies API 不可用，试试下面这个
   ```
   
   使用 document.cookie + JSON 格式：
   ```javascript
   JSON.stringify(document.cookie.split(';').map(c => {
     let [name, ...val] = c.trim().split('=');
     return {name, value: val.join('='), domain: '.08a2zp.vip', path: '/'};
   }), null, 2)
   ```
   复制输出结果，保存为 `cookies.json`。

## 方法二：EditThisCookie 扩展（推荐）

1. 安装 [EditThisCookie](https://www.editthiscookie.com/) Chrome 扩展
2. 登录投注网站
3. 点击扩展图标 → 点击 **Export** (导出) 按钮
4. 导出内容自动复制到剪贴板
5. 粘贴保存为 `cookies.json`

## 预期格式

```json
[
  {
    "name": "session_id",
    "value": "abc123...",
    "domain": ".08a2zp.vip",
    "path": "/",
    "httpOnly": true,
    "secure": true,
    "sameSite": "Lax",
    "expirationDate": 1760000000
  },
  {
    "name": "token",
    "value": "eyJ...",
    "domain": ".08a2zp.vip",
    "path": "/",
    "httpOnly": true,
    "secure": true,
    "sameSite": "Lax",
    "expirationDate": 1760000000
  }
]
```

## 文件位置

将 `cookies.json` 放在项目根目录 `D:\Architecture\` 下，或通过 `--cookies` 参数指定路径：

```
python playwright_agent.py --cookies cookies.json
python live_odds_gateway.py --cookies cookies.json
```

## 注意

- Cookies 有有效期，过期后需要重新导出
- 不要将 `cookies.json` 提交到 Git (已加入 `.gitignore`)
- 如果页面被重定向到登录页，说明 cookies 已过期或格式不对
- 导出时确保已登录状态，且未退出账号
