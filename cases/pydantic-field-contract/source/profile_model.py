"""执行器校准快照：这是自建复现，不是实际用户项目。"""

from typing import Optional, Union

from pydantic import BaseModel, Field


class Profile(BaseModel):
    # 旧版允许省略nickname；required_note则必须出现，但值可以是None。
    nickname: Optional[str]
    required_note: Optional[str] = Field(...)
    # 校准合同固定旧版优先转为int的行为；并非所有应用都应选这一策略。
    priority: Union[int, str]
