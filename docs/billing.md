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

## 配置作用域

费用配置按生图站点分别生效。主站、备用站和权威兜底站点的配置位置不同，但 `billing` 内部字段含义一致。

| 站点类型 | 配置位置 | 说明 |
| --- | --- | --- |
| 主站 | `primary_billing_json` | 对主站生图请求生效 |
| 备用站 | `fallback_api_providers` 数组元素的 `billing` 字段 | 每个备用站单独配置，按对应 Provider 生效 |
| 权威兜底站点 | `authoritative_fallback_billing_json` | 对权威兜底站点生效 |

### 主站配置

主站只填写 `billing` 对象本身，不需要再包一层 `billing`：

```json
{
  "currency": "CNY",
  "success_cost": 0.2,
  "failure_cost": 0
}
```

### 备用站配置

备用站必须写在对应 `fallback_api_providers` 元素的 `billing` 字段里。不要把备用站费用配置写到 `primary_billing_json`，否则只会影响主站。

```json
[
  {
    "name": "backup-1",
    "base_url": "https://api-backup.example.com/v1",
    "api_key": "sk-xxx",
    "capabilities": "images",
    "billing": {
      "currency": "CNY",
      "success_cost": 0.2,
      "failure_cost": 0
    }
  }
]
```

### 权威兜底站点配置

权威兜底站点同样只填写 `billing` 对象本身：

```json
{
  "currency": "CNY",
  "success_cost": 0.2,
  "failure_cost": 0
}
```

---

## 已验证站点场景

目前文档中的两类推荐配置都有真实站点数据支撑：

| 站点 | 已验证结论 | 推荐配置 |
| --- | --- | --- |
| MICU | `/dashboard/billing/subscription` 返回总额度，`/dashboard/billing/usage` 返回已用量，需要用“总额 - 用量 = 余额” | `total_url` + `usage_url`，并可配固定参考成本兜底 |
| LTCraft | `sk-` API key 可访问 OpenAI 兼容接口，但不能访问控制台余额接口；控制台虽然显示 USD，但余额数值当前可按 CNY 1:1 估算 | 固定参考成本 + `/image2 balance set` 手动余额锚点估算 |

这些结论只代表已实测站点。其它 NewAPI、One API 或二次封装站点可能改过接口、字段和单位，需要按本文的 REST Client 模板逐站验证。

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

## LTCraft 固定费用余额估算

LTCraft（`https://ai.ltcraft.cn`）当前实测情况：

- 首页是 NewAPI 风格前端。
- `sk-` API key 可访问 OpenAI 兼容接口，例如 `/v1/models`。
- `sk-` API key 不能访问 `/api/user/self`、`/api/user/amount`、`/api/user/topup/self`、`/api/user/topup/info`、`/api/token/` 等控制台接口，这些接口返回未授权。
- `/dashboard/billing/subscription` 和 `/dashboard/billing/usage` 的 OpenAI 兼容账单口径与控制台真实余额不一致，不能当作 LTCraft 真实余额。
- LTCraft 控制台界面可能显示 `USD`，但实测余额数值与人民币按 1:1 对应；很多中转站也会出现“界面显示一种单位，实际充值/扣费按站长自定义倍率换算”的情况。

因此 LTCraft 推荐使用“固定参考成本 + 手动余额锚点”。不建议为了实时余额在插件中长期保存 Web 登录 Cookie/JWT。

先配置固定参考成本，例如：

```json
{
  "currency": "CNY",
  "balance_multiplier": 1,
  "success_cost": 0.2,
  "failure_cost": 0
}
```

然后由管理员手动设置余额锚点：

```text
/image2 balance set LTCraftAI <控制台余额>
```

展示货币和换算倍率会从 LTCraft 的 `billing` 配置读取。即使控制台把余额标成 `USD`，也不要机械按美元汇率换算；当前 LTCraft 实测可以把 1 个余额数值按 1 CNY 估算，所以示例使用 `currency: CNY` 和 `balance_multiplier: 1`。

后续估算余额按以下方式更新：

```text
估算余额 = 手动锚点余额 - 锚点之后插件记录的固定成本
```

展示中会明确标注“手动锚点估算”。该数值不是站点实时余额；充值、站外消耗或后台调整后，应再次执行 `balance set` 校准。

如果 LTCraft 配在备用站，在对应 `fallback_api_providers` 元素中写：

```json
{
  "name": "LTCraftAI",
  "base_url": "https://ai.ltcraft.cn/v1",
  "api_key": "sk-xxx",
  "capabilities": "images",
  "billing": {
    "currency": "CNY",
    "balance_multiplier": 1,
    "success_cost": 0.2,
    "failure_cost": 0
  }
}
```

部署后先执行一次手动校准：

```text
/image2 balance set LTCraftAI <控制台余额>
```

后续每次固定参考成本事件都会从该锚点余额中扣减。

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
  "currency": "USD",
  "scale": 0.01,
  "balance_multiplier": 1,
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

不要直接把这个模板套到所有 NewAPI 系站点。LTCraft 的实测结果就与 MICU 不同：虽然部分 OpenAI 兼容账单接口可返回数据，但口径与控制台真实余额不一致。不同站点可能改过面板接口、字段名或单位，必须先用下面的 REST Client 模板逐站验证返回值语义。

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
  "currency": "CNY",
  "scale": 1,
  "balance_multiplier": 1,
  "success_cost": 0.2,
  "failure_cost": 0
}
```

如果 MICU 配在备用站，在对应 `fallback_api_providers` 元素中写：

```json
{
  "name": "Micu",
  "base_url": "https://api-slb.micuapi.ai/v1",
  "api_key": "sk-xxx",
  "capabilities": "images",
  "force_single_image_requests": true,
  "billing": {
    "total_url": "https://www.micuapi.ai/dashboard/billing/subscription",
    "total_json_path": "soft_limit_usd",
    "usage_url": "https://www.micuapi.ai/dashboard/billing/usage",
    "usage_json_path": "total_usage",
    "usage_scale": 0.01,
    "currency": "CNY",
    "scale": 1,
    "balance_multiplier": 1,
    "success_cost": 0.2,
    "failure_cost": 0
  }
}
```

如果 MICU 配在主站，且任务可能使用全局 `n > 1`，同时开启：

```json
{
  "primary_force_single_image_requests": true
}
```

如果 MICU 配在备用站，则在对应 `fallback_api_providers` 元素中开启 `force_single_image_requests`，上面的备用站示例已包含该字段。

---

## 字段说明

这一节按“用户要表达什么”来解释字段。英文键名只是配置文件里的名字，实际含义以中文说明为准。

### 基础字段

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `currency` | `CNY` | 插件最终展示、汇总和统计费用时使用的目标单位。通常为了看成本会填 `CNY`。 |
| `balance_multiplier` | `1` | 1 个站点余额数值折算成多少 `currency`。它不是固定汇率字段，也可以表示站长自定义充值比例。例如 1 个余额数值约等于 0.5 CNY，就填 `0.5`；LTCraft 当前按 1:1 估算，所以填 `1`。 |
| `success_cost` | `0` | 固定参考单张成功成本。成功时按实际返回图片数相乘，例如 `0.2` 表示每张成功图按 0.2 CNY 记账。 |
| `failure_cost` | `0` | 固定参考单次失败成本。失败时不乘以图片数，通常可填 `0`。 |
| `timeout` | `8` | 查询余额接口的超时时间，单位秒。 |

### 直接余额接口字段

如果接口直接返回“剩余余额”，使用这一组。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `balance_url` | - | 直接余额接口 URL。只能填真正返回“剩余余额”的接口，不要填总充值额、订阅上限或套餐额度接口。 |
| `method` | `GET` | 请求 `balance_url` 使用的方法，通常是 `GET`。 |
| `auth` | `bearer` | 鉴权方式。`bearer` 表示请求头使用 `Authorization: Bearer <key>`；`x-api-key` 表示请求头使用 `x-api-key: <key>`。 |
| `api_key` | - | 可选。余额接口需要和生图接口不同的 Key 时填写；不填时复用该 Provider 的生图 API Key。 |
| `balance_json_path` | `balance` | 从接口返回 JSON 里取余额数字的位置，例如 `data.balance`。 |
| `scale` | `1` | 余额原始数字的缩放倍率。如果接口返回 `1234` 代表 `12.34`，填 `0.01`。 |

### 总额减用量字段

如果站点没有直接余额接口，但有“总额度”和“已用量”两个接口，使用这一组。余额计算公式是：

```text
余额 = 总额度 × scale - 已用量 × usage_scale
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `total_url` | - | 总额度、总充值额或订阅上限接口 URL。MICU 实测使用这个接口。 |
| `total_method` | 同 `method` | 请求 `total_url` 使用的方法，通常不需要单独填写。 |
| `total_json_path` | `total` | 从总额接口返回 JSON 里取总额数字的位置。MICU 是 `soft_limit_usd`。 |
| `usage_url` | - | 已用量接口 URL。MICU 实测使用这个接口。 |
| `usage_method` | 同 `method` | 请求 `usage_url` 使用的方法，通常不需要单独填写。 |
| `usage_json_path` | `total_usage` | 从用量接口返回 JSON 里取已用量数字的位置。MICU 是 `total_usage`。 |
| `usage_scale` | `1` | 已用量原始数字的缩放倍率。MICU 的 `total_usage` 需要乘 `0.01`。 |
| `scale` | `1` | 总额原始数字的缩放倍率。MICU 的 `soft_limit_usd` 当前按 `1` 处理。 |

### 容易混淆的字段

| 配置目标 | 应该改哪个字段 | 例子 |
| --- | --- | --- |
| 插件最终用什么货币展示 | `currency` | 想全部按人民币汇总，填 `CNY` |
| 站点余额数值到展示单位怎么换算 | `balance_multiplier` | 1 个余额数值约等于 0.5 CNY，就填 `0.5`；1:1 就填 `1` |
| 接口返回整数但实际有小数 | `scale` 或 `usage_scale` | 接口返回 `1234` 表示 `12.34`，填 `0.01` |
| 没有余额接口，只想估算每张图成本 | `success_cost` / `failure_cost` | LTCraft 可配置成功单张 `0.2`、失败单次 `0` |

常见组合：

- MICU：使用 `total_url` + `usage_url`，按“总额度 - 已用量”算余额。
- LTCraft：使用 `success_cost` / `failure_cost` + `/image2 balance set <Provider> <amount>`，按固定成本和手动锚点估算余额。
- 真实美元余额站点：`currency` 填 `CNY`，`balance_multiplier` 填你希望使用的美元到人民币换算倍率。
- 显示 USD 但余额数值按人民币 1:1 扣减的中转站：`currency` 填 `CNY`，`balance_multiplier` 填 `1`。
- 站长自定义点数站点：`currency` 填 `CNY`，`balance_multiplier` 填“1 个余额数值约等于多少 CNY”。

数组路径用点号加下标，例如：

```text
data.items.0.balance
balance_infos.0.total_balance
```

---

## REST Client 测试模板

可以在 VSCode 新建 `tmp/billing-test.http`。下面模板用于验证接口口径，不能只看接口是否返回 200，还要和控制台余额对比语义是否一致。

### MICU

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

### LTCraft

LTCraft 的关键验证点是：`sk-` API key 能访问 OpenAI 兼容接口，但不能访问控制台余额接口；OpenAI 兼容账单接口返回值也不能当作控制台真实余额。

```http
@api_key = sk-替换成你的-key
@site_url = https://ai.ltcraft.cn
@base_url = {{site_url}}/v1

### 1. OpenAI compatible models should work
GET {{base_url}}/models
Authorization: Bearer {{api_key}}
Accept: application/json


### 2. Console user self should not be available with sk key
GET {{site_url}}/api/user/self
Authorization: Bearer {{api_key}}
Accept: application/json


### 3. Console user amount should not be available with sk key
GET {{site_url}}/api/user/amount
Authorization: Bearer {{api_key}}
Accept: application/json


### 4. OpenAI compatible subscription is not the console balance
GET {{site_url}}/dashboard/billing/subscription
Authorization: Bearer {{api_key}}
Accept: application/json


### 5. OpenAI compatible usage is not the console balance
GET {{site_url}}/dashboard/billing/usage
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

如果站点使用 LTCraft 这类手动锚点估算，再执行一次：

```text
/image2 balance set LTCraftAI <控制台余额>
```

期望结果：

- `/image2 providers` 能看到固定参考成本。
- `/image2 balance` 能显示站点余额。
- 手动锚点站点会显示“手动锚点估算”。
- `/image2 costs recent 5` 能看到最近费用事件。

---

## 常见问题

### `/dashboard/billing/subscription` 返回总充值额，不是余额

MICU 实测该接口返回总额度。请同时测试 `/dashboard/billing/usage`，并使用 `total_url` + `usage_url` 配置“总额度 - 已用量”。其它 NewAPI 系站点需要逐站确认，不能默认照搬 MICU 模板。

### `/api/usage/token/` 返回负数或 `unlimited_quota=true`

这说明当前 token 是无限额度 token，令牌级余额不可用。不要用 `data.total_available` 作为余额。

### `/api/user/self` 能看到正确余额，但插件不建议用

`/api/user/self` 通常需要 Web 登录 Cookie，不适合机器人长期自动查询。优先使用 API Key 可访问的接口。

如果只有网页登录态能看到余额，可以改用固定参考成本，并通过 `/image2 balance set <Provider> <amount>` 手动校准估算余额。单位、展示货币和换算倍率会从该 Provider 的 `billing` 配置读取。

### `/image2 balance` 查询失败，但生图正常

余额查询失败不会阻断生图。检查：

- `balance_url` 是否可由服务器访问。
- `auth` 是否正确。
- `balance_json_path` 是否匹配返回 JSON。
- 站点返回的是余额还是总额；如果是总额，应改用 `total_url` + `usage_url`。
