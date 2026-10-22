"""
LLM 统一客户端：通过 LiteLLM 切换 GPT/Claude/Qwen，temperature=0 保证可复现。
支持文本对话、视觉解析、JSON 结构化输出。
"""
import os
import base64
import json
from typing import List, Dict, Any
from litellm import completion
from app.core.config import settings


class LLMClient:
    """
    关键设计决策：所有 prompt 集中管理并带版本号；文本任务默认走配置模型，
    量大/降本任务可显式指定低成本模型（如 qwen-max），视觉任务走支持 vision 的模型。
    """

    PROMPTS = {
        "v1.0.0": {
            "qa_system": (
                "你是一位严谨的科研助手。请严格基于提供的编号片段回答问题，"
                "每句话末尾必须用 [引#N] 标注来源，N 为片段编号。"
                "禁止引用片段以外的知识，禁止编造引用。"
                "如果片段无法回答问题，请只回答“无法回答”。"
            ),
            "review_system": (
                "你是一位学术综述写作助手。请基于提供的论文片段，围绕主题生成一份"
                "结构清晰、可验证、可复现的文献综述，包含研究背景、方法、主要发现、"
                "待解决问题，并在每处关键论断后标注来源 arXiv ID。"
            ),
            "figure_caption": (
                "请为以下论文中的图表生成一段简短中文说明，概括图表展示的核心内容。"
            ),
            "formula_latex": (
                "请将图片中的数学公式转换为标准 LaTeX 代码。"
                "只输出 LaTeX，不要解释，不要 Markdown 代码块标记。"
            ),
            "chapter_tree": (
                "你是一位论文结构分析专家。下面是从 PDF 中提取的标题列表，"
                "请根据字体大小/层级把它们整理成一棵章节树，深度至少到三级。"
                "输出严格为 JSON：{\"tree\": [{\"id\", \"title\", \"level\", \"page\", \"children\": [...]}]}。"
            ),
            "references": (
                "下面是从论文参考文献部分提取的原始文本。"
                "请将其拆分为独立的参考文献条目，每条包含作者、标题、年份、来源、DOI（如有）。"
                "输出严格为 JSON：{\"references\": [{\"id\", \"raw\", \"authors\", \"title\", \"year\", \"venue\", \"doi\"}]}。"
            ),
            "drawing_system": (
                "你是一位科研绘图代码生成助手。你只输出可执行的 matplotlib Python 代码或 mermaid 文本，"
                "禁止输出解释。代码必须可复现：不使用随机数据，若需要随机必须显式设置 np.random.seed(42)；"
                "图表必须包含数据来源标注；不要包含文件 I/O、网络请求、plt.show() 或系统调用。"
            ),
        }
    }

    def __init__(self):
        self.model = settings.litellm_model
        self.temperature = settings.temperature
        self.prompt_version = settings.prompt_version
        if settings.openai_api_key:
            os.environ["OPENAI_API_KEY"] = settings.openai_api_key
        if settings.anthropic_api_key:
            os.environ["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
        if settings.dashscope_api_key:
            os.environ["DASHSCOPE_API_KEY"] = settings.dashscope_api_key

    def _system_prompt(self, key: str) -> str:
        prompts = self.PROMPTS.get(self.prompt_version, self.PROMPTS["v1.0.0"])
        return prompts[key]

    def chat(
        self,
        user_message: str,
        system_key: str = "qa_system",
        context: str | None = None,
        model: str | None = None,
    ) -> str:
        """通用对话接口；model 可覆盖为降本模型。"""
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt(system_key)}
        ]
        if context:
            messages.append({"role": "user", "content": f"参考片段：\n{context}\n\n问题：{user_message}"})
        else:
            messages.append({"role": "user", "content": user_message})

        resp = completion(
            model=model or self.model,
            messages=messages,
            temperature=self.temperature,
            top_p=1.0,
        )
        return resp.choices[0].message.content or ""

    def vision(self, image_bytes: bytes, prompt: str, model: str | None = None) -> str:
        """视觉模型调用，image_bytes 转为 base64 data URL。"""
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        mime = "image/png"
        data_url = f"data:{mime};base64,{b64}"
        messages: List[Dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ]
        resp = completion(
            model=model or self.model,
            messages=messages,
            temperature=self.temperature,
        )
        return resp.choices[0].message.content or ""

    def json_chat(
        self,
        user_message: str,
        system_key: str,
        model: str | None = None,
    ) -> Dict[str, Any]:
        """要求模型输出 JSON 并解析。"""
        content = self.chat(user_message, system_key=system_key, model=model)
        if content.startswith("```"):
            content = content.strip("`")
            if content.startswith("json"):
                content = content[4:].strip()
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return {"raw": content, "error": "json_decode_failed"}

    def generate_review(self, topic: str, context: str) -> str:
        """生成综述。"""
        prompt = f"主题：{topic}\n\n参考片段：\n{context}\n\n请生成综述。"
        return self.chat(prompt, system_key="review_system")
