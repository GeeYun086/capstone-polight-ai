from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


# Spring(Java)과의 JSON 계약은 camelCase, 내부 파이썬 코드는 snake_case로 쓰기 위한 공통 베이스.
# populate_by_name=True라서 snake_case 키워드로 생성해도, camelCase JSON을 파싱해도 둘 다 동작한다.
class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
