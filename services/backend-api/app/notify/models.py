"""统一消息中枢的消息模型。

生产者只描述「发生了什么」，不构造任何飞书 / 企微 payload——否则每加一个投递目标，
每个生产者都要跟着改。渲染成各家原生格式是 channel 的事。

段落模型（segments）沿用 openclaw-bridge 的 build_feishu_card：文字和图按文档顺序
交错排成一条消息，而不是拆成好几个气泡。那套结构在飞书链路上已经跑了几个月。
"""

from __future__ import annotations

import base64
import hashlib
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

LEVELS: dict[str, int] = {"info": 0, "warn": 1, "error": 2, "fatal": 3}
Level = Literal["info", "warn", "error", "fatal"]

LEVEL_ICONS: dict[str, str] = {"info": "🔵", "warn": "🟡", "error": "🔴", "fatal": "⛔"}

# 图片上限取三家里最紧的那个：企微群机器人 base64 图 2MB。
# 飞书 im/v1/images 宽松得多，但判据统一在最紧处，省得同一条消息发得出 A 发不出 B。
MAX_IMAGE_BYTES = 2 * 1024 * 1024

# 判「标题开头是不是一个图标」。So=其他符号（绝大多数 emoji）、Sk=修饰符号。
_ICON_CATEGORIES = {"So", "Sk"}
# ⚠️ 只看类别是不够的：U+2139（ℹ）在 Unicode 里的类别是 **Ll（小写字母）**，
# U+00A9（©）是 So 但常被当字母用——真正要问的是「它是不是按 emoji 呈现的」，
# 而那件事由后面的变体选择符 U+FE0F 决定。少了这一条，ℹ️ 和 ⚠️ 开头的标题会
# 被判成「没有图标」，于是照样叠一个级别图标上去（2026-08-31 被测试当场抓到）。
_EMOJI_VARIATION_SELECTOR = "\ufe0f"


def level_at_least(level: str, minimum: str) -> bool:
    return LEVELS.get(level, 0) >= LEVELS.get(minimum, 0)


class NotifyField(BaseModel):
    """一行「名：值」。各 channel 自己决定渲染成表格、列表还是纯文本。"""

    name: str = Field(max_length=64)
    value: str = Field(max_length=512)


class NotifyImage(BaseModel):
    """随消息附带的图。ref 是段落里引用它的名字。

    只收 PNG，且在**入口**就校验格式与大小。收敛前这层校验在 gold_spread 的
    _upload_charts 里，收敛时必须跟着搬过来——否则坏图会一路带到 IM API 才报错，
    那时消息已经写进 outbox，失败变成异步的、当场看不出来。
    """

    ref: str = Field(max_length=64)
    caption: str = Field(default="", max_length=120)
    png_base64: str = Field(max_length=4_000_000)

    @model_validator(mode="after")
    def _check_png(self) -> "NotifyImage":
        try:
            data = base64.b64decode(self.png_base64, validate=True)
        except Exception:
            raise ValueError(f"image {self.ref} is not valid base64") from None
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError(f"image {self.ref} is not a PNG")
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError(
                f"image {self.ref} is {len(data)} bytes, over the {MAX_IMAGE_BYTES} limit"
            )
        return self


class NotifySegment(BaseModel):
    """一个段落。kind 决定用哪个字段：

    - ``text``   → ``text``，markdown 子集（各 channel 自行适配方言）；
      置 ``preformatted=True`` 表示这段是**排好版的纯文本**，渲染时不得当 markdown 解析
    - ``fields`` → ``fields``，键值对
    - ``image``  → ``image_ref``，指向 ``Notification.images`` 里的某张图
    """

    kind: Literal["text", "fields", "image"]
    text: str = Field(default="", max_length=8000)
    # 排版里含 *、|、# 这类字符时必须置 True，否则会被 markdown 吃掉或变成标题。
    # gold_spread 的价差排版就是这种情况——旧的 build_alert_card 用 plain_text 而非
    # lark_md 正是为此，收敛时这个语义必须跟着搬过来，不能丢。
    preformatted: bool = False
    fields: list[NotifyField] = Field(default_factory=list, max_length=40)
    image_ref: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def _check_payload(self) -> "NotifySegment":
        if self.kind == "text" and not self.text.strip():
            raise ValueError("text segment is empty")
        if self.kind == "fields" and not self.fields:
            raise ValueError("fields segment is empty")
        if self.kind == "image" and not self.image_ref.strip():
            raise ValueError("image segment has no image_ref")
        return self


class NotifyLink(BaseModel):
    text: str = Field(max_length=64)
    url: str = Field(max_length=1024)


# 飞书卡片头的主题色。取值抄自官方枚举，**必须在入口校验**：写错一个值飞书会整张卡片
# 拒收，而 feishu.send 的降级策略是退回纯文本——于是「配色写错」在观测面上长得和
# 「卡片发不出去」一模一样，事后完全查不出来。宁可在这里当场报错。
HEADER_TEMPLATES = frozenset(
    {
        "blue", "wathet", "turquoise", "green", "yellow", "orange",
        "red", "carmine", "violet", "purple", "indigo", "grey", "default",
    }
)

# 标签底色。官方颜色枚举还有 -50 ~ -900 的深浅变体和 RGBA 自定义，这里只放基础色：
# 白名单窄一点最多是拒掉一个能用的颜色，放宽了则是整张卡片静默降级成纯文本。
TAG_COLORS = frozenset(
    {
        "neutral", "blue", "carmine", "green", "indigo", "lime", "grey", "orange",
        "purple", "red", "sunflower", "turquoise", "violet", "wathet", "yellow",
    }
)

# 按钮样式，取值同上。default 是描边按钮，primary 是蓝色实心。
BUTTON_STYLES = frozenset(
    {
        "default", "primary", "danger", "text",
        "primary_text", "danger_text", "primary_filled", "danger_filled", "laser",
    }
)


class NotifyTag(BaseModel):
    """标题后缀标签。飞书最多 3 个，企微没有对应组件（降级成标题行里的行内代码）。"""

    text: str = Field(max_length=24)
    color: str = "neutral"

    @model_validator(mode="after")
    def _check_color(self) -> "NotifyTag":
        if self.color not in TAG_COLORS:
            raise ValueError(f"unknown tag color: {self.color}")
        return self


class NotifyButton(BaseModel):
    """一个跳转按钮。只支持 open_url——回调按钮需要一条入站链路，中枢现在没有。"""

    text: str = Field(max_length=64)
    url: str = Field(max_length=1024)
    style: str = "default"

    @model_validator(mode="after")
    def _check_style(self) -> "NotifyButton":
        if self.style not in BUTTON_STYLES:
            raise ValueError(f"unknown button style: {self.style}")
        return self


class Notification(BaseModel):
    """一条待投递的通知。

    ``dedup_key`` 是幂等闸门：同一个 key 重复提交只会产生一条 outbox 行。
    生产者应当用「业务事件的自然主键」当 dedup_key（例如 ``gold:wrong_price:<event_id>``），
    留空则按内容摘要生成——那样重试会被当成新消息，只适合本来就无所谓重复的通知。
    """

    source: str = Field(max_length=64)
    event: str = Field(max_length=128)
    level: Level = "info"
    title: str = Field(max_length=200)
    subtitle: str = Field(default="", max_length=200)
    # 留空则按 level 映射颜色（现有告警全部走这条，行为不变）。生产者要绿色成功卡、
    # 灰色例行通报这类「级别说明不了的语义」时才显式给。
    theme: str = ""
    # 飞书上限 3 个，超了整张卡片被拒。max_length 让 pydantic 在入口就拒，
    # 而不是悄悄截掉第 4 个——生产者以为发出去了、实际没有，是最难查的一类。
    tags: list[NotifyTag] = Field(default_factory=list, max_length=3)
    summary: str = Field(default="", max_length=2000)
    segments: list[NotifySegment] = Field(default_factory=list, max_length=60)
    images: list[NotifyImage] = Field(default_factory=list, max_length=12)
    link: NotifyLink | None = None
    buttons: list[NotifyButton] = Field(default_factory=list, max_length=5)
    dedup_key: str = Field(default="", max_length=200)
    occurred_at: datetime | None = None

    @model_validator(mode="after")
    def _fill_defaults(self) -> "Notification":
        if self.occurred_at is None:
            self.occurred_at = datetime.now(timezone.utc)
        if self.theme and self.theme not in HEADER_TEMPLATES:
            raise ValueError(f"unknown header theme: {self.theme}")
        refs = {image.ref for image in self.images}
        for segment in self.segments:
            if segment.kind == "image" and segment.image_ref not in refs:
                raise ValueError(f"image segment references unknown ref: {segment.image_ref}")
        if not self.dedup_key:
            self.dedup_key = self._content_dedup_key()
        return self

    def _content_dedup_key(self) -> str:
        digest = hashlib.sha256()
        digest.update(self.source.encode())
        digest.update(self.event.encode())
        digest.update(self.title.encode())
        digest.update(self.summary.encode())
        for segment in self.segments:
            digest.update(segment.kind.encode())
            digest.update(segment.text.encode())
            for field in segment.fields:
                digest.update(field.name.encode())
                digest.update(field.value.encode())
        # 同一内容在不同时刻发生仍是两条通知，所以把时间也算进去；
        # 真正需要「重发不重复」的生产者必须自己给 dedup_key。
        digest.update(str(self.occurred_at).encode())
        return f"auto:{self.source}:{digest.hexdigest()[:32]}"

    def storable_payload(self) -> dict[str, Any]:
        """入库用的 payload：剥掉图片字节，只留长度做审计。

        一张 PNG 的 base64 是几十万字符，直接进 JSONB 会把这一列撑爆——
        gold_spread_alerts 的 _strip_chart_bytes 已经踩过这个坑。
        代价是重试时没有图，按纯文本降级。
        """
        payload = self.model_dump(mode="json", exclude={"images"})
        payload["images"] = [
            {
                "ref": image.ref,
                "caption": image.caption,
                "base64_characters": len(image.png_base64),
            }
            for image in self.images
        ]
        return payload

    @classmethod
    def from_stored(cls, payload: dict[str, Any]) -> "Notification":
        """从库里读回来重试用。图片已在入库时剥离，因此重建出来的是无图版本。"""
        rebuilt = dict(payload)
        rebuilt["images"] = []
        rebuilt["segments"] = [
            segment for segment in rebuilt.get("segments") or []
            if segment.get("kind") != "image"
        ]
        if not rebuilt.get("dedup_key"):
            rebuilt["dedup_key"] = f"restored:{uuid.uuid4().hex}"
        return cls.model_validate(rebuilt)

    def display_title(self) -> str:
        """带级别图标的标题——但生产者已经写了图标就不再叠加。

        2026-08-31 发现：gold-spread-monitor 的每一类标题首行本来就自带图标
        （✅ ⛔ 🔴 🟢 🧾 🧪 ℹ️ ⚠️），中枢再按 level 前置一个，飞书卡片标题就成了
        「🔴 🔴 疑似错单成交｜沪金 AU2612」。收敛前的 build_alert_card 直接用生产者
        那一行，没有这个问题。

        生产者的图标往往比级别更具体（🧾 收盘复盘 / 🧪 历史回放验证 说的是「哪一类」
        而不是「多严重」），所以保留它、跳过级别图标，而不是反过来剥掉它。

        ⚠️ 这个判断必须只有这一处。飞书卡片头和企微 markdown 首行是「同一件事」，
        各写一套迟早会出现两边标题不一致。
        """
        title = self.title.strip()
        if title and (
            unicodedata.category(title[0]) in _ICON_CATEGORIES
            or title[1:2] == _EMOJI_VARIATION_SELECTOR
        ):
            return title
        icon = LEVEL_ICONS.get(self.level, "")
        return f"{icon} {title}".strip()

    def all_buttons(self) -> list[NotifyButton]:
        """按钮的唯一口径：老的单个 ``link`` 折算成第一个按钮，后面接 ``buttons``。

        ``link`` 不删——doc-sync-worker 的 notify_client 还在用它，而那是另一个镜像、
        另一次部署。两个字段并存时必须只有这一处折算，否则飞书卡片、企微 markdown、
        纯文本兜底三处迟早出现「有的渲染了 link 有的没有」。
        """
        buttons: list[NotifyButton] = []
        if self.link is not None:
            buttons.append(NotifyButton(text=self.link.text, url=self.link.url))
        buttons.extend(self.buttons)
        return buttons

    def plain_text(self) -> str:
        """所有 channel 的共同兜底：富格式发不出去时至少把字发出去。"""
        lines = [self.display_title()]
        if self.subtitle.strip():
            lines.append(self.subtitle.strip())
        if self.tags:
            lines.append(" ".join(f"[{tag.text}]" for tag in self.tags))
        if self.summary.strip():
            lines.append(self.summary.strip())
        for segment in self.segments:
            if segment.kind == "text":
                lines.append(segment.text.strip())
            elif segment.kind == "fields":
                lines.extend(f"{field.name}：{field.value}" for field in segment.fields)
            elif segment.kind == "image":
                caption = next(
                    (image.caption for image in self.images if image.ref == segment.image_ref),
                    "",
                )
                lines.append(f"🖼️ {caption}".strip() if caption else "🖼️ 图片")
        for button in self.all_buttons():
            lines.append(f"{button.text}：{button.url}")
        return "\n".join(line for line in lines if line).strip()
