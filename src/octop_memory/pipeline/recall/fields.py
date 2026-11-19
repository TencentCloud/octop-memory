"""Lexical expansion for common topic/field pairs and evidence checks for value lookups."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Vocabulary:
    match: re.Pattern[str]
    terms: tuple[str, ...]


def _vocab(pattern: str, *terms: str) -> Vocabulary:
    return Vocabulary(re.compile(pattern, re.IGNORECASE), terms)


_FILE = _vocab(r"附件|文件|上传|上载|attachment|\bupload|\bfiles?\b", "附件", "文件", "上传", "attachment", "upload")
_LOG = _vocab(r"日志|(?:运行|系统|服务|执行)记录|\blogs?\b", "日志", "运行记录", "log")
_BACKUP = _vocab(r"备份|\bbackups?\b", "备份", "backup")
_WEEKLY = _vocab(
    r"周报|(?:每周|每星期|每礼拜|一周|本周)(?:的)?(?:工作)?(?:报告|汇总|总结|小结)|\bweekly (?:report|update|summary)",
    "周报",
    "每周总结",
    "weekly report",
)
_MONTHLY = _vocab(
    r"月报|(?:每月|本月)(?:的)?(?:工作)?(?:报告|汇总|总结|小结)|\bmonthly (?:report|summary)", "月报", "monthly report"
)

# Contact roles are independent of the field (e.g. phone or e-mail).
_OWNER = _vocab(
    r"负责人|责任人|项目经理|牵头人|\b(?:owner|project lead|project manager)\b", "负责人", "项目经理", "owner"
)
_SIGNER = _vocab(r"签字人|签署人|签批人|\bsignator(?:y|ies)\b", "签字人", "签署人", "signatory")
_SUPPORT = _vocab(r"客服|服务台|帮助台|\b(?:help ?desk|customer support)\b", "客服", "服务台", "help desk")
_ON_CALL = _vocab(r"值班|\bon[- ]call\b", "值班", "on-call")
_ROLES = (_OWNER, _SIGNER, _SUPPORT, _ON_CALL)

_NUMBER = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万半]+)"
_SIZE_VALUE = rf"{_NUMBER}\s*(?:[kmgt]i?b(?![a-z])|bytes?(?![a-z])|兆(?:字节)?|吉字节|千字节|字节)"
_DURATION_VALUE = rf"{_NUMBER}\s*(?:个?月|分钟|小时|天|周|年|days?\b|weeks?\b|months?\b|years?\b)"
_KEEP = r"保留|留存|保存|存放|存储|留多久|留几|retain|retention|\bkept\b|\bkeep\b"
_LIMIT = r"上限|封顶|最大|最多|限额|限制|不得超过|不能超过|不超过|不得大于|maximum|max\b|limit|up to"
_RETENTION_ACTION = re.compile(_KEEP + r"|清理|清除|删除|过期|期限|expiration|expire|purge", re.I)
_FORMAT = (
    r"(?<![a-z0-9_])(?:markdown|asciidoc|xlsx?|docx?|pptx?|csv|tsv|pdf|html|json|ya?ml|txt|odt|rtf)(?![a-z0-9_])"
    r"|纯文本|电子表格|表格文件|富文本"
)
_PHONE = r"(?:联系电话|电话(?:号码)?|手机号(?:码)?|手机|座机|热线|\b(?:phone|telephone|mobile|hotline)(?: number)?\b)"
_EMAIL = r"(?:电子邮箱|邮箱(?:地址)?|邮件地址|\be-?mail(?: address)?\b)"
# Require a labeled number. Dates and long order IDs elsewhere in the same atom are insufficient.
_DIAL = r"(?:可拨(?:打)?|请拨(?:打)?|致电)"
_PHONE_VALUE = (
    "(?:" + _PHONE + "|" + _DIAL + ")" + r"\s*(?:(?:为|是|号码|请拨(?:打)?|is|at|number)\s*)?[:：=]?\s*"
    r"(?:\+|\()?\d(?:[ ().-]{0,3}\d){6,14}(?![ ().-]{0,3}\d)"
)
_MAIL_VALUE = r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}"
_CLAUSE = re.compile(r"[。！？!?；;\n]|(?<!\d)\.(?=\s|$)")  # noqa: RUF001
_CONTACT_CLAUSE = re.compile(r"[，,、]")
_CONTACT_WORDS = re.compile(_PHONE + "|" + _EMAIL, re.I)
_CONTACT_FILLER = re.compile(
    r"那个项目|这个项目|应该拨|要拨|请拨打|拨打|要打|拨|联系|请问|请告诉我|告诉我|发给我|给我|是多少|是什么|是哪个|多少|什么|哪个|号码|的|是|呢|吗|"
    r"\b(?:what|which|is|the|a|an|for|of|that|this|project|number|address|please|give|me|s)\b",
    re.I,
)
_DIFFERENT_SIZE_FIELD = re.compile(r"数量|几个|个数|次数|速度|速率|时长|带宽|how many|speed|bandwidth", re.I)
_UPLOAD_OR_LIMIT = re.compile(
    _LIMIT + r"|上传|上载|传文件|发文件|可.{0,3}多大|能.{0,3}多大|upload|attach.{0,12}size", re.I
)
_NON_CONTACT_LOOKUP = re.compile(
    r"流程|会议|回访|诈骗|型号|修改|更改|更换|申请|接听|接电话|打电话|拨电话|推销|营销|policy|change|update|meeting|model",
    re.I,
)
_COMPOUND_ROLE = re.compile("|".join(role.match.pattern for role in _ROLES), re.I)


@dataclass(frozen=True)
class AnswerField:
    ask: re.Pattern[str]
    value: re.Pattern[str]
    terms: tuple[str, ...]
    qualifier: re.Pattern[str] | None = None


def _field(ask: str, value: str, terms: tuple[str, ...], qualifier: str | None = None) -> AnswerField:
    return AnswerField(
        re.compile(ask, re.I), re.compile(value, re.I), terms, re.compile(qualifier, re.I) if qualifier else None
    )


_SIZE = _field(
    _LIMIT + r"|能有多大|多大|大小|多少\s*(?:兆|[kmgt]b)|how (?:big|large)|size",
    _SIZE_VALUE + r"|不限(?:大小|容量)|(?:大小|容量)(?:不限|无限制)|unlimited",
    ("上限", "最大", "limit"),
    _LIMIT + r"|不限|无限制|unlimited",
)
_RETENTION = _field(
    r"期限|保留期|留存期|保存期|多久|多长时间|几(?:个)?(?:天|月|周|年)|多少(?:天|个?月|周|年)|how long|retention",
    _DURATION_VALUE + r"|永久|长期|forever|indefinitely",
    ("保留", "留存", "retention"),
    _KEEP + r"|到期清理|后(?:删除|清除|清理)|expires?",
)
_DOCUMENT_FORMAT = _field(
    r"格式|哪(?:种|类)文档|(?:什么|哪种)(?:文件|文档)(?:类型)?|什么类型|\b(?:format|file type)\b",
    _FORMAT,
    ("格式", "导出", "format"),
)
_PHONE_NUMBER = _field(
    _PHONE + r"\s*(?:(?:是|有)?(?:多少|什么|哪个)|给我|发我|[？?]?\s*$)|(?:拨|要打|拨打)(?:哪个|什么)号(?:码)?"
    r"|\b(?:phone number|mobile number|telephone number)\b",
    _PHONE_VALUE,
    ("电话", "手机号", "phone"),
)
_EMAIL_ADDRESS = _field(
    _EMAIL + r"\s*(?:(?:是|有)?(?:多少|什么|哪个)|[？?]?\s*$)|\be-?mail address\b",
    _MAIL_VALUE,
    ("邮箱", "邮件地址", "email"),
)


@dataclass(frozen=True)
class FieldLookup:
    field: AnswerField
    subject: Vocabulary | None
    contact_context: tuple[str, ...] = ()

    def expand(self, topic: str, terms: tuple[str, ...]) -> tuple[str, ...]:
        # Preserve the whole subject. Fragments of an identified compound
        # (e.g. 记录 in 运行记录) can swamp both bounded pools before admission.
        if not self.subject:
            return tuple(dict.fromkeys((*terms, *self.field.terms)))
        phrases = tuple(match[0].casefold() for match in self.subject.match.finditer(topic))
        residual = (term for term in terms if not any(term != phrase and term in phrase for phrase in phrases))
        return tuple(dict.fromkeys((*phrases, *self.subject.terms, *self.field.terms, *residual)))

    def evidence(self, assertion: str) -> tuple[str, ...]:
        """Return assertion clauses that contain the field and its requested subject."""
        supported = []
        for sentence in _CLAUSE.split(assertion):
            parts = _CONTACT_CLAUSE.split(sentence)
            for index, part in enumerate(parts):
                if self._supports(part):
                    supported.append(part)
                elif index and self.field in (_PHONE_NUMBER, _EMAIL_ADDRESS):
                    # Preserve the common "owner is X, phone is Y" construction.
                    # Keep preceding clauses out when the value already stands alone.
                    pair = parts[index - 1] + "，" + part
                    if self.field.value.search(part) and self._supports(pair):
                        supported.append(pair)
        return tuple(supported)

    def _supports(self, clause: str) -> bool:
        if self.field.qualifier and not self.field.qualifier.search(clause):
            return False
        if self.subject and not self.subject.match.search(clause):
            return False
        return any(self._value_matches(clause, value) for value in self.field.value.finditer(clause))

    def _value_matches(self, clause: str, value: re.Match[str]) -> bool:
        if self.field not in (_PHONE_NUMBER, _EMAIL_ADDRESS):
            return True
        prefix = clause[: value.start()]
        if self.subject:
            roles = list(_COMPOUND_ROLE.finditer(prefix))
            if not roles or not self.subject.match.fullmatch(roles[-1][0]):
                return False
            between = prefix[roles[-1].end() :]
            if any(part.strip() for part in _CONTACT_CLAUSE.split(between)[1:]):
                return False
        return all(term in prefix.casefold() for term in self.contact_context)


def field_lookup(topic: str) -> FieldLookup | None:
    """Analyze the residual topic before question scaffolding or units are stripped."""
    for field in (_PHONE_NUMBER, _EMAIL_ADDRESS):
        if field.ask.search(topic) and not _NON_CONTACT_LOOKUP.search(topic):
            role = next((r for r in _ROLES if r.match.search(topic)), None)
            rest = _CONTACT_WORDS.sub(" ", topic)
            if role:
                rest = role.match.sub(" ", rest)
            context = tuple(re.findall(r"[㐀-鿿]+|[a-z0-9]+", _CONTACT_FILLER.sub(" ", rest).casefold()))
            return FieldLookup(field, role, context)
    for subject, field in (
        (_FILE, _SIZE),
        (_LOG, _RETENTION),
        (_BACKUP, _RETENTION),
        (_WEEKLY, _DOCUMENT_FORMAT),
        (_MONTHLY, _DOCUMENT_FORMAT),
    ):
        if subject.match.search(topic) and field.ask.search(topic):
            if field is _RETENTION and not _RETENTION_ACTION.search(topic):
                continue
            if field is _SIZE and (_DIFFERENT_SIZE_FIELD.search(topic) or not _UPLOAD_OR_LIMIT.search(topic)):
                continue
            return FieldLookup(field, subject)
    return None
