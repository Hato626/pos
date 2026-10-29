# 餐厅双流程打印扩展

后端作为独立服务部署，使用同一数据库和鉴权模块。前端位于 `front/src/app/settings/print-settings.component.ts`，已集成到餐厅的“设置 → 打印”。

- `schema.sql`：餐厅绑定、助手队列清单、事务事件表及触发器。仅对已开启相应开关的餐厅记录新事件，安装时不回放历史订单。
- `server.py`：登录餐厅的绑定 API、事件转换、助手领取和确认。运行于独立容器的 8022，反向代理为 `/api/print-workflow/`。
- `workflow.py`：后厨增量、退菜、改菜与结账凭证内容；部分支付不会触发结算单。
- `RestaurantPrintAgent.cs`：Windows 10 / .NET Framework 4.8 程序，使用系统驱动和 Windows 队列按后台每队列尺寸打印中文标签（50×60、30×60、60×50、60×30、原60×40 mm）。没有外部 NuGet 依赖。
- `test_workflow.py`：只允许对名为 `printing_test` 的隔离 PostgreSQL 库运行，验证事务回滚、租户隔离、增量打印、支付和并发领取。

构建 Windows 助手：使用 Windows 已有的 .NET Framework 4 编译器 `csc.exe` 编译 `RestaurantPrintAgent.cs`，引用 System.Drawing、System.Windows.Forms、System.Web.Extensions、System.Net.Http，不需要外部 NuGet 包。使用 `winexe` 目标避免启动控制台窗口。

运行渲染检查：`RestaurantPrintAgent.exe --self-test <输出目录>`，生成中文后厨单、结算单 PNG 和检查报告，不连接打印机。

打印助手只主动连接 HTTPS，无本地监听端口。绑定由店主／管理员保存；令牌属于单个餐厅，可在原后台“设置 → 打印”撤销。

任务领取使用数据库行锁，任务及事件消费在事务内提交。成功表示已提交 Windows 队列，无法证明纸张已物理输出。网络确认重试不会再次打印；进程中断后不确定任务需要员工确认和补打，服务端不会自动回收并重印 claimed 任务。

限制：当前一套餐厅绑定选择一个电脑助手，再把两个角色分别映射到该电脑可见的队列；两台独立电脑的按角色分配、税务发票、自动退款打印尚未实现。长订单按多张标签输出。纸张及驱动必须由店内实机校准。

停止自动生成任务可在绑定页关闭两个开关；停止 `printing` 容器会保留事务事件等待后续处理。完全移除扩展前，先关闭各餐厅开关，并备份数据库，再移除两个 `deployment_*_print` 触发器。不要直接删除含队列和事件的表。

`PUT /paper-settings` 仅店主/管理员可写；尺寸按餐厅、助手和队列保存，限制为支持的尺寸，校验队列归属。`GET /paper-profiles` 允许有订单读取权限的员工获取本餐厅的预览配置。领取时快照尺寸，任务列表显示实际领取尺寸。新助手v2支持动态纸张、窄纸换行和按高度分页；旧助手仅能处理60×40任务。
