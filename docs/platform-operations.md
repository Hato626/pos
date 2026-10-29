# 平台经营与 Adyen 分账

平台管理员从 `/platform/login` 登录，进入 `/platform`。餐厅账号仍从 `/login` 登录。

## 管理能力

- 新建餐厅和店主账号、编辑联系方式、启用或停用餐厅、重置店主密码。
- 默认抽佣比例及餐厅独立比例；首次收款或创建在线支付链接时固定订单费率。
- 按日期、餐厅、币种查看收款、退款、佣金、餐厅应得、手续费和预计利润，导出明细。
- 平台联系方式、条款地址、注册开关、顾客端地址、Adyen 测试／正式配置。
- 录入实际渠道费用、提交全额或部分退款、查看管理员操作日志。

金额存储为最小货币单位，比例存储为基点（100 基点 = 1%）。小费不抽佣。分次付款使用累计舍入，避免每次舍入累积误差。历史付款以零佣金导入，不追溯收费。现金及其他支付渠道的佣金是应收款，不代表平台已收取。

预计利润 = 佣金 − 渠道手续费。费用未核对时使用配置的估算值并标记待核对。币种分开统计，日期使用 UTC。该页面不包含餐厅食材、人工或平台运营成本。

## Adyen 配置

1. 在 Adyen 开通适用于平台分账的账户；普通商户账户本身不足以支持本流程。餐厅须完成 Adyen 认证，并具备 `receivePayments`、`receiveFromPlatformPayments`、`sendToTransferInstrument` 能力。
2. 部署时在私有环境文件中设置 `PLATFORM_SECRETS_KEY`，用 `cryptography.fernet.Fernet.generate_key()` 生成。必须备份此密钥；丢失后无法解密已保存的凭据。不要写入源码或公开环境文件。
3. 在平台设置录入 Merchant account、Balance platform、平台责任余额账户、Checkout API key、Balance Platform API key、Webhook HMAC key。正式环境还需 Checkout live endpoint 前缀。密钥写入后不回显；空值保留已有密钥。
4. 在餐厅管理绑定各环境的 Balance account 和 Account holder，并验证。银行名称、尾号、Transfer instrument ID 是核对备注；银行账户开户、认证、提现计划和 sweeps 在 Adyen 后台管理，本项目不主动发起银行提现。
5. 在 Adyen 创建 Standard webhook，地址为 `{顾客端地址}/api/platform-payments/adyen/webhook`，开启 HMAC。需要开启自动捕获的 `CAPTURE` 通知及 `CAPTURE_FAILED`、退款、拒付相关事件。只收到 `AUTHORISATION` 不会把订单标记为已支付。
6. 保存顾客端 HTTPS 地址并启用正式收款。测试收款只允许在独立部署设置 `PLATFORM_ALLOW_TEST_PAYMENTS=true` 后开启；不要在真实餐厅使用测试模式。

扫码订单通过 Pay by Link 跳转到 Adyen 托管页面，平台不会接触卡号。服务端计算金额和分账，验证签名及原订单后记账。回跳页面只查询服务端状态，不根据 URL 宣告付款成功。当前集成范围是餐桌订单；外卖入口保留原有支付渠道。

成功捕获将触发原有结账状态与打印事件。退款以原比例申请分账退款，只有回调确认后记入账本；退款不会自动重开订单或重打票据。拒付及支付金额与当前账单不一致时标记待核对。实际银行到账及最终费用以 Adyen 对账为准。Webhook 幂等键和事务锁防止重复记账，退款请求使用持久化幂等引用。

## 构建、迁移与验证

使用仓库 `back/Dockerfile` 和 `front/Dockerfile.prod` 构建。生产后端覆盖默认启动命令为 `uvicorn app.main:app --host 0.0.0.0 --port 8020`，不启用 `--reload`。现有打印扩展源码在 `deployment-printing/`，前端代理要求同网络内存在 `pos-printing:8022`。保留 uploads 和数据库持久卷。

上线前备份数据库、上传文件和私有配置，先执行 `python -m app.migrate`，再用 `python -m app.migrate --check` 确认无待执行迁移，最后替换服务镜像。新迁移是增量变更，禁止重建业务库或运行演示数据清理脚本。旧版本镜像可用于应用回退，新增财务表应保留。

`back/tests/platform_operations_isolated.py` 拒绝生产数据库，只允许 `platform_operations_test`，在隔离 PostgreSQL 中测试抽佣、费用、权限、HMAC、并发、退款和停用。Adyen 请求被替换为模拟响应。另运行原有支付、安全、扫码和打印流程测试。前端需生产编译及手机／桌面浏览器冒烟验证。语言完整性检查：`python scripts/check-i18n-locale-parity.py`。

生产启用前仍须使用自己的 Adyen 测试账户完成真实沙盒联调，以及核对正式商户、认证和银行提现设置；自动化模拟测试不替代这些步骤。

## 公共页品牌状态

`PublicTenantCacheService` 共享餐厅 ID 和 slug 的品牌缓存及正在进行的请求。切换菜单、预订、排队、外卖、会员和反馈页面时同步保留品牌；过期数据后台刷新，403/404 清除别名，避免短暂显示默认主题。缓存仅存在当前页面内存中，不写入持久存储。
