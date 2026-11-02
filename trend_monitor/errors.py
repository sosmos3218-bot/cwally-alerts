"""오류 구분. 데이터 실패는 종료코드 1, 입력 오류는 2."""

from __future__ import annotations


class TrendError(Exception):
    code = 1


class TrendDataError(TrendError):
    """시세가 없거나 오래되었거나 부족합니다. 가격을 만들지 않습니다."""

    code = 1


class UserInputError(TrendError):
    code = 2
