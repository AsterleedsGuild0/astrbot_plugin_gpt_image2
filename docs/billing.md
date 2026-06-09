# 费用观测配置

本文档说明如何为每个生图站点配置余额观测和固定参考成本。

---

## 先选择费用来源

插件支持三种费用来源。请按站点实际接口选择一种主要来源，再按需填写固定参考成本作为展示和兜底。

| 来源 | 适用场景 | 关键字段 |
| --- | --- | --- |
| 直接余额 | 接口直接返回剩余余额 | `balance_url` + `balance_json_path` |
| 总额减用量 | 一个接口返回总额度，另一个接口返回已用量 | `total_url` + `usage_url` |
| 固定参考 | 没有可用费用接口，只能估算单张/单次成本 | `success_cost` / `failure_cost` |

`balance_url` 严格表示“余额查询接口”。如果某个接口返回的是总充值额、总额度或订阅上限，请使用 `total_url`，不要写进 `balance_url`。

插件计算优先级：

1. 有 `balance_url` 时，按生图前后余额差计算真实成本。
2. 没有 `balance_url`，但有 `total_url` 和 `usage_url` 时，按“总额 - 已用量”得到当前余额，再用生图前后余额差计算成本。
3. 余额查询失败、字段解析失败或余额差异常时，使用 `success_cost` / `failure_cost` 兜底。
4. 没有余额或总额接口时，只使用固定参考成本。

---

## 配置放在哪里

主站写在 `primary_billing_json`：

```json
{
  "currency": "USD",
  "success_cost": 0.03,
  "failure_cost": 0
}
```

备用站写在 `fallback_api_providers` JSON 数组元素的 `billing` 字段里：

```json
[
  {
    "name": "backup-1",
    "base_url": "https://api-backup.example.com/v1",
    "api_key": "sk-xxx",
    "capabilities": "images",
    "billing": {
      "currency": "USD",
      "success_cost": 0.03,
      "failure_cost": 0
    }
  }
]
```

权威兜底站点写在 `authoritative_fallback_billing_json`。

---

## 固定参考成本配置

如果站点没有可用余额 API，或你只想先展示大致成本，配置固定参考成本即可：

```json
{
  "currency": "CNY",
  "success_cost": 0.2,
  "failure_cost": 0
}
```

效果：

- `/image2 providers` 显示固定参考成本。
- 生图成功按 `success_cost × 实际返回图片数` 记录费用。
- 如果无法取得实际返回图片数，则退回配置里的 `n`。
- 失败尝试按 `failure_cost` 记录费用，不乘以 `n`。

---

## 单接口余额配置

如果站点有一个接口直接返回剩余额度，配置 `balance_url` 和 `balance_json_path`。

例如返回：

```json
{
  "data": {
    "balance": 1234
  }
}
```

如果 `1234` 表示 `12.34 USD`，配置：

```json
{
  "balance_url": "https://api.example.com/api/user/self",
  "method": "GET",
  "auth": "bearer",
  "balance_json_path": "data.balance",
  "balance_unit": "USD",
  "currency": "USD",
  "scale": 0.01,
  "cost_multiplier": 1,
  "success_cost": 0.03,
  "failure_cost": 0
}
```

含义：

```text
接口原始值 1234 * scale 0.01 = 12.34 USD
```

---

## MICU 双接口余额配置

MICU（`https://www.micuapi.ai`）当前实测情况：

- `/dashboard/billing/subscription` 返回总额度或总充值额。
- `/dashboard/billing/usage` 返回已用量。
- 可用余额需要用“总额度 - 已用量”计算。
- 实测存在原生 `n=2` 只返回 1 张但按站点余额差扣 2 张费用的情况；使用 MICU 且需要多图时，建议给该 Provider 开启 `force_single_image_requests`。该开关只表示“每次上游请求使用 `n=1`”，不会把整次任务限制为 1 张；全局 `n=2` 时仍可能通过两次 `n=1` 请求得到 2 张图。

不要直接把这个模板套到所有 NewAPI 系站点。不同站点可能改过面板接口、字段名或单位，必须先用下面的 REST Client 模板逐站验证返回值语义。

MICU 示例返回：

```json
{
  "soft_limit_usd": 150
}
```

```json
{
  "total_usage": 10000
}
```

实际余额：

```text
150 * 1 - 10000 * 0.01 = 50 CNY
```

配置：

```json
{
  "total_url": "https://www.micuapi.ai/dashboard/billing/subscription",
  "total_json_path": "soft_limit_usd",
  "usage_url": "https://www.micuapi.ai/dashboard/billing/usage",
  "usage_json_path": "total_usage",
  "usage_scale": 0.01,
  "balance_unit": "CNY",
  "currency": "CNY",
  "scale": 1,
  "cost_multiplier": 1,
  "success_cost": 0.2,
  "failure_cost": 0
}
```

如果 MICU 配在主站，且任务可能使用全局 `n > 1`，同时开启：

```json
{
  "primary_force_single_image_requests": true
}
```

如果 MICU 配在备用站，在对应 `fallback_api_providers` 元素中写：

```json
{
  "name": "Micu",
  "base_url": "https://api-slb.micuapi.ai/v1",
  "force_single_image_requests": true
}
```

---

## 字段说明

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `balance_url` | - | 直接余额接口 URL，只能表示剩余余额 |
| `method` | `GET` | `balance_url` 请求方法：`GET` 或 `POST` |
| `auth` | `bearer` | 鉴权方式：`bearer` 或 `x-api-key` |
| `api_key` | - | 可选；余额接口使用独立 Key 时填写，省略时复用 Provider API Key |
| `balance_json_path` | `balance` | 从余额接口 JSON 中取数的点路径 |
| `total_url` | - | 总额度、总充值额或订阅上限接口 URL |
| `total_method` | 同 `method` | `total_url` 请求方法 |
| `total_json_path` | `total` | 从总额接口 JSON 中取数的点路径 |
| `usage_url` | - | 已用量接口 URL |
| `usage_method` | 同 `method` | `usage_url` 请求方法 |
| `usage_json_path` | `total_usage` | 从已用量接口 JSON 中取数的点路径 |
| `usage_scale` | `1` | 已用量换算倍率 |
| `balance_unit` | `USD` | 站点余额单位 |
| `currency` | 同 `balance_unit` | 最终展示/统计货币 |
| `scale` | `1` | 余额或总额原始值换算倍率 |
| `cost_multiplier` | `1` | 站点余额单位到展示货币的换算倍率 |
| `timeout` | `8` | 余额接口超时时间，单位秒 |
| `success_cost` | `0` | 固定参考单张成功成本；成功时按实际返回图片数相乘 |
| `failure_cost` | `0` | 固定参考单次失败成本；失败时不乘以图片数 |

数组路径用点号加下标，例如：

```text
data.items.0.balance
balance_infos.0.total_balance
```

---

## REST Client 测试模板

可以在 VSCode 新建 `tmp/billing-test.http`：

```http
@api_key = sk-替换成你的-key
@site_url = https://www.micuapi.ai
@base_url = {{site_url}}/v1

### 1. MICU subscription
GET {{site_url}}/dashboard/billing/subscription
Authorization: Bearer {{api_key}}
Accept: application/json


### 2. MICU usage
GET {{site_url}}/dashboard/billing/usage
Authorization: Bearer {{api_key}}
Accept: application/json


### 3. Token usage
GET {{site_url}}/api/usage/token/
Authorization: Bearer {{api_key}}
Accept: application/json


### 4. OpenAI compatible models
GET {{base_url}}/models
Authorization: Bearer {{api_key}}
Accept: application/json
```

---

## 配置后验证

部署配置后依次执行：

```text
/image2 providers
/image2 balance
/image2 draw 简单测试图，一只白色小猫
/image2 costs
/image2 costs recent 5
```

期望结果：

- `/image2 providers` 能看到固定参考成本。
- `/image2 balance` 能显示站点余额。
- `/image2 costs recent 5` 能看到最近费用事件。

---

## 常见问题

### `/dashboard/billing/subscription` 返回总充值额，不是余额

MICU 实测该接口返回总额度。请同时测试 `/dashboard/billing/usage`，并使用 `total_url` + `usage_url` 配置“总额度 - 已用量”。其它 NewAPI 系站点需要逐站确认，不能默认照搬 MICU 模板。

### `/api/usage/token/` 返回负数或 `unlimited_quota=true`

这说明当前 token 是无限额度 token，令牌级余额不可用。不要用 `data.total_available` 作为余额。

### `/api/user/self` 能看到正确余额，但插件不建议用

`/api/user/self` 通常需要 Web 登录 Cookie，不适合机器人长期自动查询。优先使用 API Key 可访问的接口。

### `/image2 balance` 查询失败，但生图正常

余额查询失败不会阻断生图。检查：

- `balance_url` 是否可由服务器访问。
- `auth` 是否正确。
- `balance_json_path` 是否匹配返回 JSON。
- 站点返回的是余额还是总额；如果是总额，应改用 `total_url` + `usage_url`。
