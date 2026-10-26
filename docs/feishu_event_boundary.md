# 飞书事件接入边界与部署条件

`install_feishu_integration(app, policy, verifier=..., queue=...)` 接受仅由后端启动代码提供的可信适配器。默认及只提供一个适配器时保持关闭（503）；默认启动方式没有提供适配器，因此不会自动启用接入。

实际应用启动函数提供仅keyword的注入入口：

```python
app = create_app(
    store, config,
    feishu_event_verifier=trusted_verifier,
    feishu_sync_queue=durable_queue,
)
```

上述对象需由受控后端bootstrap创建；不是HTTP参数、环境中的任意可加载代码或浏览器配置。这里只提供注入接口，不创建或启用任何真实适配器。完整create_app集成测试覆盖默认503、注入后202、无效事件403及重复投递去重。

- verifier 接收原始字节和请求头，负责真实飞书协议的签名/解密、时间窗、防重放、租户及目录根授权、事件类型转换。返回 `CatalogChange`；任何异常或无效归一化事件均403，且不会入队。
- queue 必须原子持久化，以 event_id 去重；相同内容的重复投递返回同一job_id，冲突内容必须拒绝。持久化失败或返回空job_id均503，允许发送方重试。提交后响应丢失也必须安全重试。
- 202仅证明适配器报告已入队，不证明已读取飞书或刷新快照。状态 `events_configured` 仅反映依赖已注入，不是外部联通健康证明。
- 后台消费重试、死信处理、全量校准调度及可观测性由实际queue/worker提供。scheduled_sync及cloud_writeback仍关闭。
- 尚无URL验证challenge实现；只有能归一化为目录变更的已验证事件可入队。真实接入需部署适配器按飞书协议提供独立验证握手流程。

`tests/test_feishu_event_boundary.py` 使用明确的假验证器及SQLite测试队列，验证HTTP边界、跨测试应用/队列重建的去重、提交前及提交后失败重试、冲突事件拒绝。它们不是飞书验签实现，也不验证生产队列或真实飞书互通。生产队列必须使用同等契约验收，尤其多进程并发去重、持久化恢复和worker失败重试。

没有读取凭证、访问飞书或启用外部回写。
