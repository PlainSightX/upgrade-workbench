# JWT 配置与鉴权迁移合同

将冻结上游的Pydantic 1.10.15迁移到2.6.4，保留鉴权库的配置加载与使用行为。
原项目用于FastAPI服务的访问/刷新令牌、header和cookie鉴权，不是完整身份提供商。

- 默认header位置、15分钟访问/30天刷新有效期及cookie CSRF默认开启保持。
- 配置接受上游已支持的列表/集合/元组位置和方法；每项合法性、字符串去空白与非空约束保持。
- StrictBool/StrictInt/StrictStr不隐式放宽；Optional字段显式None的既有行为保留。
- 有效期接受False、整数秒及timedelta，False表示无exp；True必须拒绝。
- 配置回调接受Pydantic模型或键值元组列表，非法配置保持ValidationError，非法回调保持TypeError。
- 有效访问令牌可访问受保护路由；缺失、错误签名、refresh冒充access、denylist命中均仍拒绝。
- 自定义header名称与无前缀模式保持；cookie POST的CSRF双提交匹配规则不降低。
- 使用Pydantic v2原生配置/验证接口，不以pydantic.v1兼容命名空间或跳过校验完成任务。

允许修改config.py、auth_config.py、auth_jwt.py。检查、锁、异常定义及原始快照不可修改。
本合同不评价RSA、WebSocket、全部浏览器cookie属性、任意版本兼容或生产安全认证。
