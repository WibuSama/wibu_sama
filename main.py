# -*- coding: utf-8 -*-
"""
FastAPI + MetaGPT backend with:
- /init: Start clarification session
- /ws/{session_id}: WebSocket to clarify with virtual PM (JSON questions)
- /confirm/{session_id}: Confirm and trigger MetaGPT team
- /result/{session_id}: Retrieve result
- Serves static UI from /static/index.html
"""
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional, Union
import asyncio
import json
import os
import re
from datetime import datetime

# ==========================
# REFLECTIVE LLM AGENT CORE
# ==========================

# Định nghĩa các trạng thái của orchestrator
class OrchestratorState:
    INITIALIZING = "initializing"
    ANALYZING = "analyzing"
    ASKING_QUESTION = "asking_question"
    EVALUATING_ANSWER = "evaluating_answer"
    ENRICHING_CONTEXT = "enriching_context"
    PLANNING_NEXT_ACTION = "planning_next_action"
    SUMMARIZING = "summarizing"
    FINALIZING = "finalizing"
    ERROR = "error"

class LLMAction:
    ASK_FOLLOW_UP = "ask_follow_up"
    CALL_TOOL = "call_tool"
    CONCLUDE = "conclude"
    SUMMARIZE = "summarize"
    SEARCH_MORE = "search_more"
    REFINE_UNDERSTANDING = "refine_understanding"
    REQUEST_SPECIFIC_INFO = "request_specific_info"

# Cấu trúc cho một cycle của orchestrator
class OrchestrationCycle:
    def __init__(self, state: str, context: Dict[str, Any]):
        self.state = state
        self.context = context
        self.start_time = datetime.utcnow()
        self.end_time = None
        self.reasoning = []
        self.tools_called = []
        self.llm_actions = []
        self.summary = None
        self.next_state = None
        
    def add_reasoning(self, reasoning: str):
        """Thêm một bước lý luận vào cycle hiện tại"""
        timestamp = datetime.utcnow().isoformat()
        self.reasoning.append({"timestamp": timestamp, "reasoning": reasoning})
        
    def add_tool_call(self, tool_name: str, params: Dict[str, Any], result: Any):
        """Lưu lại thông tin về công cụ đã gọi"""
        timestamp = datetime.utcnow().isoformat()
        self.tools_called.append({
            "timestamp": timestamp,
            "tool": tool_name,
            "params": params,
            "result_summary": str(result)[:100] + "..." if isinstance(result, str) and len(str(result)) > 100 else result
        })
    
    def add_llm_action(self, action_type: str, details: Dict[str, Any]):
        """Lưu lại hành động của LLM"""
        timestamp = datetime.utcnow().isoformat()
        self.llm_actions.append({
            "timestamp": timestamp,
            "action_type": action_type,
            "details": details
        })
    
    def complete(self, next_state: str, summary: str):
        """Hoàn thành cycle hiện tại"""
        self.end_time = datetime.utcnow()
        self.next_state = next_state
        self.summary = summary

class LLMOrchestrator:
    """
    LLM-driven Orchestration Engine - tự động hóa quá trình phân tích, đánh giá và
    đưa ra quyết định về các bước tiếp theo trong quá trình làm rõ yêu cầu.
    """
    
    def __init__(self, llm_instance=None):
        """
        Khởi tạo orchestrator
        
        Args:
            llm_instance: Instance của LLM để sử dụng, nếu không có sẽ tạo mới
        """
        from metagpt.llm import LLM
        self.llm = llm_instance or LLM()
        self.cycles = []
        self.current_cycle = None
        self.session_data = {
            "requirement": None,
            "chat_history": [],
            "technical_details": {},
            "context_enrichments": [],
            "final_summary": None,
            "ready_to_conclude": False
        }
    
    def _start_new_cycle(self, state: str):
        """Bắt đầu một cycle mới với trạng thái đã cho"""
        # Lưu cycle hiện tại vào history nếu có
        if self.current_cycle:
            self.cycles.append(self.current_cycle)
            
        # Tạo cycle mới
        self.current_cycle = OrchestrationCycle(
            state=state,
            context={
                "timestamp": datetime.utcnow().isoformat(),
                "requirement": self.session_data["requirement"],
                "chat_history_length": len(self.session_data["chat_history"])
            }
        )
        return self.current_cycle
    
    async def initialize(self, requirement: str, team_data: Optional[List[Dict]] = None):
        """
        Khởi tạo session với yêu cầu ban đầu
        
        Args:
            requirement: Yêu cầu ban đầu từ người dùng
            team_data: Thông tin về team nếu có
        """
        self._start_new_cycle(OrchestratorState.INITIALIZING)
        self.session_data["requirement"] = requirement
        self.session_data["team_data"] = team_data or []
        
        # Phân tích yêu cầu ban đầu
        self.current_cycle.add_reasoning("Bắt đầu phiên làm rõ yêu cầu")
        
        # Tự động làm giàu ngữ cảnh với MCP nếu có thể
        if USE_MCP_ENRICHMENT:
            self.current_cycle.add_reasoning("Làm giàu ngữ cảnh ban đầu với MCP tools")
            self.current_cycle.state = OrchestratorState.ENRICHING_CONTEXT
            
            try:
                # Phân tích yêu cầu ban đầu để làm giàu ngữ cảnh
                enriched_data = await enrich_context_with_mcp(requirement, [])
                self.session_data["context_enrichments"].append({
                    "timestamp": datetime.utcnow().isoformat(),
                    "enrichment_data": enriched_data
                })
                
                # Lưu thông tin về công cụ đã gọi
                self.current_cycle.add_tool_call(
                    "enrich_context_with_mcp", 
                    {"text": requirement}, 
                    "Làm giàu ngữ cảnh ban đầu"
                )
                
                # Thêm reasoning
                for reasoning in enriched_data.get("reasoning", []):
                    self.current_cycle.add_reasoning(reasoning)
            except Exception as e:
                self.current_cycle.add_reasoning(f"Lỗi khi làm giàu ngữ cảnh ban đầu: {str(e)}")
        
        self.current_cycle.complete(
            OrchestratorState.ASKING_QUESTION,
            "Khởi tạo session thành công"
        )
    
    async def generate_first_question(self) -> Dict[str, Any]:
        """
        Sinh câu hỏi đầu tiên để bắt đầu làm rõ yêu cầu
        
        Returns:
            Dict chứa câu hỏi chính và các câu hỏi làm rõ
        """
        self._start_new_cycle(OrchestratorState.ASKING_QUESTION)
        self.current_cycle.add_reasoning("Sinh câu hỏi đầu tiên")
        
        # Xây dựng prompt
        prompt = await self._build_first_question_prompt()
        
        # Gọi LLM để sinh câu hỏi đầu tiên
        llm_result = await self.llm.aask(prompt)
        self.current_cycle.add_llm_action(LLMAction.ASK_FOLLOW_UP, {"prompt": "First question", "result": llm_result[:100] + "..."})
        
        # Parse kết quả từ LLM (không fallback)
        clean_response = llm_result.strip()
        if clean_response.startswith("\n") and clean_response.endswith("\n"):
            clean_response = clean_response[3:-3].strip()
            if clean_response.startswith("json"):
                clean_response = clean_response[4:].strip()
        
        question_data = json.loads(clean_response)
        self.session_data["last_question"] = question_data["main_question"]
        
        self.current_cycle.complete(
            OrchestratorState.ASKING_QUESTION,
            "Đã sinh câu hỏi đầu tiên thành công"
        )
        
        return question_data
            
    async def _build_first_question_prompt(self) -> str:
        """Xây dựng prompt cho câu hỏi đầu tiên"""
        # Lấy enrichment data gần đây nhất nếu có
        enrichment_text = ""
        if USE_MCP_ENRICHMENT and self.session_data["context_enrichments"]:
            latest_enrichment = self.session_data["context_enrichments"][-1]["enrichment_data"]
            enrichment_text = format_enriched_context(latest_enrichment)
        
        prompt = f"""
Bạn là Kỹ sư Trưởng. Hãy đọc yêu cầu sau và sinh ra 1 câu hỏi chính để làm rõ về các chi tiết kỹ thuật, kèm 2-3 câu hỏi phụ nếu cần.

YÊU CẦU: {self.session_data['requirement']}
THÔNG TIN TEAM: {json.dumps(self.session_data['team_data'], ensure_ascii=False)}

{enrichment_text}

HƯỚNG DẪN CHI TIẾT:
1. Tập trung vào các khía cạnh TRIỂN KHAI KỸ THUẬT và CHI TIẾT PHÁT TRIỂN cụ thể
2. Hỏi về stack công nghệ, kiến trúc, database, API, integration, workflow kỹ thuật...
3. Hỏi chi tiết đến mức các class/function quan trọng nếu có thể
4. TRÁNH hỏi về các giới hạn (limit) hoặc thời gian xử lý tối đa
5. Hỏi về các requirement kỹ thuật chi tiết mà developer cần để triển khai

**BẮT BUỘC**: Chỉ được trả lời bằng tiếng Việt, không sử dụng tiếng Anh, không giải thích thêm, không markdown/code block, không thêm bất kỳ ký tự nào ngoài JSON.

Trả về JSON:
{{
  "main_question": "...",
  "clarifying_questions": ["...", "..."]
}}
"""
        return prompt
    
    async def process_user_answer(self, user_answer: str, last_question: str) -> Dict[str, Any]:
        """
        Xử lý câu trả lời của người dùng, làm giàu ngữ cảnh, và quyết định hành động tiếp theo
        
        Args:
            user_answer: Câu trả lời của người dùng
            last_question: Câu hỏi cuối cùng được hỏi
            
        Returns:
            Dict chứa thông tin về action tiếp theo và các câu hỏi mới (nếu có)
        """
        self._start_new_cycle(OrchestratorState.EVALUATING_ANSWER)
        self.current_cycle.add_reasoning(f"Xử lý câu trả lời của người dùng: '{user_answer[:50]}...'")
        
        # Lưu câu hỏi và câu trả lời vào chat history
        self.session_data["chat_history"].append({
            "question": last_question,
            "answer": user_answer,
            "timestamp": datetime.utcnow().isoformat()
        })
        
        # 1. Làm giàu ngữ cảnh với MCP tools nếu có thể
        if USE_MCP_ENRICHMENT:
            self.current_cycle.state = OrchestratorState.ENRICHING_CONTEXT
            try:
                # Làm giàu ngữ cảnh với công cụ MCP
                self.current_cycle.add_reasoning("Làm giàu ngữ cảnh với MCP tools")
                
                enriched_data = await enrich_context_with_mcp(
                    user_answer=user_answer,
                    chat_history=self.session_data["chat_history"]
                )
                
                # Lưu kết quả enrichment
                self.session_data["context_enrichments"].append({
                    "timestamp": datetime.utcnow().isoformat(),
                    "enrichment_data": enriched_data
                })
                
                # Lưu thông tin về công cụ đã gọi
                self.current_cycle.add_tool_call(
                    "enrich_context_with_mcp", 
                    {"text": user_answer}, 
                    "Làm giàu ngữ cảnh"
                )
                
                # Thêm reasoning từ quá trình làm giàu ngữ cảnh
                for reasoning in enriched_data.get("reasoning", []):
                    self.current_cycle.add_reasoning(reasoning)
                
            except Exception as e:
                self.current_cycle.add_reasoning(f"Lỗi khi làm giàu ngữ cảnh: {str(e)}")
        
        # 2. Đánh giá câu trả lời và lập kế hoạch hành động tiếp theo
        self.current_cycle.state = OrchestratorState.PLANNING_NEXT_ACTION
        evaluation_result = await self._evaluate_and_plan_next_action(user_answer, last_question)
        
        # 3. Thực hiện hành động tiếp theo dựa trên kết quả đánh giá
        next_action = evaluation_result.get("next_action", LLMAction.ASK_FOLLOW_UP)
        
        if next_action == LLMAction.CONCLUDE:
            # Kết thúc quá trình làm rõ yêu cầu
            self.session_data["ready_to_conclude"] = True
            self.session_data["final_summary"] = evaluation_result.get("updated_summary", "")
            
            self.current_cycle.complete(
                OrchestratorState.FINALIZING,
                "Hoàn thành quá trình làm rõ yêu cầu"
            )
            
            return {
                "action": "conclude",
                "updated_summary": evaluation_result.get("updated_summary", ""),
                "technical_analysis": evaluation_result.get("technical_analysis", {}),
                "is_completed": True
            }
            
        else:
            # Sinh câu hỏi tiếp theo dựa trên đánh giá
            next_question = evaluation_result.get("next_question", "")
            clarifying_questions = evaluation_result.get("clarifying_questions", [])
            
            self.current_cycle.complete(
                OrchestratorState.ASKING_QUESTION,
                f"Chuẩn bị câu hỏi tiếp theo: '{next_question[:50]}...'"
            )
            
            return {
                "action": "ask_follow_up",
                "main_question": next_question,
                "clarifying_questions": clarifying_questions,
                "updated_summary": evaluation_result.get("updated_summary", ""),
                "is_completed": False
            }
    
    async def _evaluate_and_plan_next_action(self, user_answer: str, last_question: str) -> Dict[str, Any]:
        """
        Đánh giá câu trả lời của người dùng và lập kế hoạch hành động tiếp theo
        
        Args:
            user_answer: Câu trả lời của người dùng
            last_question: Câu hỏi cuối cùng được hỏi
            
        Returns:
            Dict với thông tin đánh giá và kế hoạch hành động
        """
        # Xây dựng prompt dựa trên lịch sử và ngữ cảnh làm giàu
        prompt = await self._build_evaluation_prompt(user_answer, last_question)
        
        # Gọi LLM để phân tích và đưa ra quyết định
        llm_result = await self.llm.aask(prompt)
        self.current_cycle.add_llm_action(LLMAction.REFINE_UNDERSTANDING, {"prompt": "Evaluate and plan next action", "result": llm_result[:100] + "..."})
        
        # Parse kết quả từ LLM
        clean_response = llm_result.strip()
        if clean_response.startswith("\n") and clean_response.endswith("\n"):
            clean_response = clean_response[3:-3].strip()
            if clean_response.startswith("json"):
                clean_response = clean_response[4:].strip()
        
        evaluation_result = json.loads(clean_response)
        
        # Cập nhật technical details từ kết quả đánh giá
        if "technical_analysis" in evaluation_result:
            self.session_data["technical_details"].update(evaluation_result["technical_analysis"])
        
        return evaluation_result
    
    async def _build_evaluation_prompt(self, user_answer: str, last_question: str) -> str:
        """
        Xây dựng prompt để đánh giá câu trả lời và lập kế hoạch hành động tiếp theo
        
        Args:
            user_answer: Câu trả lời của người dùng
            last_question: Câu hỏi cuối cùng được hỏi
            
        Returns:
            Prompt cho LLM
        """
        # Format chat history
        chat_history_text = ""
        for i, exchange in enumerate(self.session_data["chat_history"], 1):
            chat_history_text += f"Q{i}: {exchange.get('question', '')}\n"
            chat_history_text += f"A{i}: {exchange.get('answer', '')}\n\n"
        
        # Lấy enrichment data gần đây nhất nếu có
        enrichment_text = ""
        if USE_MCP_ENRICHMENT and self.session_data["context_enrichments"]:
            latest_enrichment = self.session_data["context_enrichments"][-1]["enrichment_data"]
            enrichment_text = format_enriched_context(latest_enrichment)
        
        # Xây dựng prompt
        prompt = f"""
Bạn là Kỹ sư Trưởng với kiến thức sâu rộng về thiết kế kỹ thuật, kiến trúc hệ thống, và phát triển phần mềm. Hãy phân tích chi tiết thông tin hiện tại:

YÊU CẦU BAN ĐẦU:
{self.session_data["requirement"]}

CÂU HỎI CUỐI CÙNG:
{last_question}

CÂU TRẢ LỜI MỚI NHẤT:
{user_answer}

LỊCH SỬ TRAO ĐỔI:
{chat_history_text}

{enrichment_text}

NHIỆM VỤ CỦA BẠN:
1. PHÂN TÍCH câu trả lời của người dùng từ góc độ kỹ thuật
2. ĐÁNH GIÁ thông tin hiện có đã đủ để phát triển ứng dụng chưa 
3. QUYẾT ĐỊNH hành động tiếp theo:
   - Nếu thông tin ĐÃ ĐỦ: Tổng hợp tất cả thành bản technical requirement hoàn chỉnh
   - Nếu thông tin CHƯA ĐỦ: Tạo câu hỏi tiếp theo để làm rõ các chi tiết kỹ thuật còn thiếu

TIÊU CHÍ ĐỦ THÔNG TIN:
- Chi tiết kĩ thuật đủ để kỹ sư bắt đầu triển khai
- Có hiểu rõ kiến trúc tổng thể của hệ thống
- Technical stacks và framework đã rõ ràng
- Critical APIs và workflows đã được định nghĩa
- Database schema hoặc data structures đã rõ ràng
- Business logic và nghiệp vụ chính đã được làm rõ

Trả về CHÍNH XÁC dưới dạng JSON sau:
{{
  "updated_summary": "Tổng hợp hiểu biết hiện tại về yêu cầu kỹ thuật",
  "technical_analysis": {{
    "architecture": "Kiến trúc đề xuất",
    "components": ["Component1", "Component2"],
    "data_flow": "Mô tả luồng dữ liệu",
    "class_diagram": "ASCII diagram của class diagram chính",
    "sequence_diagram": "ASCII diagram của sequence diagram chính",
    "api_endpoints": ["API1", "API2"],
    "database_schema": "Mô tả schema",
    "integrations": ["Integration1", "Integration2"]
  }},
  "next_action": "ask_more hoặc sufficient",
  "next_question": "Câu hỏi kỹ thuật chi tiết về triển khai hoặc workflow",
  "clarifying_questions": [
    {
      "question": "Chi tiết kỹ thuật về triển khai của chức năng?",
      "technical_focus": "triển khai",
      "importance": "Cao" 
    },
    {
      "question": "Chi tiết cụ thể về thuật toán/công nghệ/API cần sử dụng?",
      "technical_focus": "công nghệ",
      "importance": "Trung bình"
    }
  ],
  "final_summary": "Tóm tắt cuối cùng với chi tiết kỹ thuật",
  "is_completed": true,
  "role_discussions": [
    { "role": "Quản lý Sản phẩm", "highlights": "Điểm nhấn từ Quản lý Sản phẩm" },
    { "role": "Kiến trúc sư", "highlights": "Điểm nhấn từ Kiến trúc sư" },
    { "role": "Kỹ sư", "highlights": "Điểm nhấn từ Kỹ sư" }
  ]
}}
"""
        return prompt
    
    async def finalize_requirements(self) -> Dict[str, Any]:
        """
        Tổng hợp và hoàn thiện các yêu cầu kỹ thuật cuối cùng
        
        Returns:
            Dict chứa yêu cầu kỹ thuật cuối cùng đã hoàn thiện
        """
        self._start_new_cycle(OrchestratorState.SUMMARIZING)
        self.current_cycle.add_reasoning("Tổng hợp yêu cầu kỹ thuật cuối cùng")
        
        if not self.session_data["ready_to_conclude"]:
            self.current_cycle.add_reasoning("Cố gắng tổng hợp mặc dù chưa sẵn sàng kết thúc")
        
        # Xây dựng prompt tổng hợp
        prompt = self._build_finalization_prompt()
        
        # Gọi LLM để tổng hợp
        final_result = await self.llm.aask(prompt)
        self.current_cycle.add_llm_action(LLMAction.SUMMARIZE, {"prompt": "Finalize requirements", "result": final_result[:100] + "..."})
        
        # Xử lý kết quả
        clean_response = final_result.strip()
        if clean_response.startswith("\n") and clean_response.endswith("\n"):
            clean_response = clean_response[3:-3].strip()
            if clean_response.startswith("json"):
                clean_response = clean_response[4:].strip()
        
        final_requirements = json.loads(clean_response)
        self.session_data["final_requirements"] = final_requirements
        
        self.current_cycle.complete(
            OrchestratorState.FINALIZING,
            "Hoàn thành tổng hợp yêu cầu kỹ thuật"
        )
        
        return final_requirements
    
# ==========================
# MCP CONTEXT ENRICHER
# ==========================

# Flag toàn cục để kiểm tra xem MCP enrichment có khả dụng không
USE_MCP_ENRICHMENT = True  # Mặc định là True vì code đã được nhúng vào main.py

async def extract_urls_from_text(text: str) -> List[str]:
    """
    Extract URLs from user text
    
    Args:
        text: User input text
        
    Returns:
        List of found URLs
    """
    import re
    url_pattern = r'https?://[^\s]+'
    return re.findall(url_pattern, text)

async def enrich_context_with_mcp(
    user_answer: str, 
    chat_history: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Enrich context by calling MCP tools with the user's latest answer
    Only enrich if user_answer contains technical keywords (not just confirmation/empty)
    """
    result = {
        "semantic_search_results": {},
        "code_search_results": {},
        "web_results": {},
        "intelligent_insights": [],
        "reasoning": []
    }

    # Các câu xác nhận hoặc không chứa thông tin kỹ thuật
    confirmation_phrases = [
        "ok", "xong", "done", "đồng ý", "confirm", "xác nhận", "không bổ sung", "không", "no", "yes", "đủ", "được rồi"
    ]
    # Nếu user chỉ trả lời xác nhận hoặc câu ngắn không có từ khóa kỹ thuật thì bỏ qua enrich
    if not user_answer or user_answer.strip().lower() in confirmation_phrases or len(user_answer.strip()) < 10:
        return result

    # Step 1: Extract technical keywords from user's answer
    try:
        # Nếu có LLM-based extraction thì dùng, không thì fallback
        keywords = extract_keywords_simple_fallback(user_answer)
        print(f"✓ Keywords extracted: {keywords}")
    except Exception as e:
        print(f"✗ Keyword extraction failed: {e}")
        raise  # No fallback - raise exception to debug
    
    # Nếu không có keyword kỹ thuật thì không enrich
    if not keywords:
        print("ℹ No technical keywords found - skipping enrichment")
        return result

    # Step 2: Perform semantic search for each keyword (tối đa 3)
    for keyword in keywords[:3]:
        try:
            # Importing from mcp_client instead of mcp_tools
            from mcp_client import semantic_search
            print(f"⏳ Running semantic search for: {keyword}")
            sem_results = await semantic_search(query=keyword, context_type="code", limit=3)
            result["semantic_search_results"][keyword] = sem_results
            print(f"✓ Semantic search completed for: {keyword}")
        except Exception as e:
            print(f"✗ Semantic search failed for {keyword}: {e}")
            # Continue with other tools, don't abort on failure
    
    # Step 3: Search codebase for specific code implementations (tối đa 2)
    for keyword in keywords[:2]:
        try:
            # Importing from mcp_client instead of mcp_tools
            from mcp_client import search_codebase
            print(f"⏳ Running code search for: {keyword}")
            code_results = await search_codebase(query=keyword, limit=2)
            result["code_search_results"][keyword] = code_results
            print(f"✓ Code search completed for: {keyword}")
        except Exception as e:
            print(f"✗ Code search failed for {keyword}: {e}")
            # Continue with other tools, don't abort on failure
    
    # Step 4: Extract and crawl URLs if present in user answer
    try:
        urls = await extract_urls_from_text(user_answer)
        if urls:
            print(f"✓ URLs found in text: {urls}")
            # Importing from mcp_client instead of mcp_tools
            from mcp_client import crawl_and_summarize
            for url in urls:
                try:
                    print(f"⏳ Crawling webpage: {url}")
                    web_result = await crawl_and_summarize(url=url)
                    result["web_results"][url] = web_result
                    print(f"✓ Web crawl completed for: {url}")
                except Exception as e:
                    print(f"✗ Web crawl failed for {url}: {e}")
                    # Continue with other URLs, don't abort on failure
        else:
            print("ℹ No URLs found in user answer")
    except Exception as e:
        print(f"✗ URL extraction failed: {e}")
        # Continue with the result we have so far
    
    print(f"✓ Enrichment complete. Results: {len(result['semantic_search_results'])} semantic searches, {len(result['code_search_results'])} code searches, {len(result['web_results'])} web crawls")
    return result

def extract_keywords_simple_fallback(text: str) -> List[str]:
    """
    Simple keyword extraction fallback when LLM-based extraction isn't available
    
    Args:
        text: Text to extract keywords from
        
    Returns:
        List of keywords
    """
    import re
    from collections import Counter
    
    # Remove punctuation and convert to lowercase
    text = re.sub(r'[^\w\s]', ' ', text.lower())
    
    # Split into words
    words = text.split()
    
    # Filter out common stop words (basic list)
    stop_words = {'a', 'an', 'the', 'and', 'but', 'if', 'or', 'because', 'as', 'until', 'while', 
                 'of', 'at', 'by', 'for', 'with', 'about', 'to', 'từ', 'với', 'các', 'những',
                 'là', 'và', 'nếu', 'hoặc', 'bởi', 'vì', 'cho', 'trong', 'này', 'khi'}
    
    filtered_words = [word for word in words if word not in stop_words and len(word) > 2]
    
    # Count word frequencies
    word_counts = Counter(filtered_words)
    
    # Get top 5 most frequent words as keywords
    top_keywords = [word for word, _ in word_counts.most_common(5)]
    
    return top_keywords

def format_enriched_context(enriched_data: Dict[str, Any]) -> str:
    """
    Format enriched context into a string that can be added to LLM prompt
    
    Args:
        enriched_data: The data returned by enrich_context_with_mcp
        
    Returns:
        Formatted string with enriched context
    """
    formatted_text = "## ENRICHED CONTEXT FROM MCP TOOLS\n\n"
    
    # Add semantic search results
    if enriched_data.get("semantic_search_results"):
        formatted_text += "### SEMANTIC SEARCH RESULTS\n"
        
        for keyword, results in enriched_data["semantic_search_results"].items():
            formatted_text += f"\n#### SEARCH FOR: '{keyword}'\n"
            
            if "results" in results and results["results"]:
                for i, item in enumerate(results["results"][:3], 1):  # Limit to top 3 results per keyword
                    formatted_text += f"{i}. **{item.get('file', 'Unknown file')}**:\n"
                    formatted_text += f"   {item.get('content', 'No content')[:200]}...\n\n"
            else:
                formatted_text += "No relevant results found.\n\n"
    
    # Add code search results
    if enriched_data.get("code_search_results"):
        formatted_text += "### CODE IMPLEMENTATION REFERENCES\n"
        
        for keyword, results in enriched_data["code_search_results"].items():
            formatted_text += f"\n#### IMPLEMENTATION FOR: '{keyword}'\n"
            
            if "results" in results and results["results"]:
                for i, item in enumerate(results["results"][:2], 1):  # Limit to top 2 results per keyword
                    formatted_text += f"{i}. **{item.get('file', 'Unknown file')}**:\n"
                    formatted_text += f"\n{item.get('content', 'No content')[:300]}...\n\n"
            else:
                formatted_text += "No implementation references found.\n\n"
    
    # Add web results
    if enriched_data.get("web_results"):
        formatted_text += "### WEB REFERENCES\n\n"
        
        for url, result in enriched_data["web_results"].items():
            formatted_text += f"**Source: {url}**\n\n"
            
            if "summary" in result:
                formatted_text += f"{result['summary'][:500]}...\n\n"
            
            if "relevant_sections" in result:
                formatted_text += "**Key sections:**\n"
                for section in result["relevant_sections"][:3]:  # Limit to top 3 sections
                    formatted_text += f"- {section[:200]}...\n"
                formatted_text += "\n"
    
    return formatted_text

async def generate_enriched_prompt(
    original_prompt: str,
    user_answer: str,
    chat_history: List[Dict[str, Any]]
) -> str:
    """
    Generate an enriched prompt by adding MCP tool results to the original prompt
    
    Args:
        original_prompt: The original prompt to be enhanced
        user_answer: The latest user answer
        chat_history: Complete chat history
        
    Returns:
        An enhanced prompt with additional context
    """
    # Get enriched context from MCP tools
    print("⏳ Starting context enrichment with MCP tools...")
    enriched_data = await enrich_context_with_mcp(user_answer, chat_history)
    print("✓ Context enrichment completed")
    
    # Format the enriched data for the prompt (non-async function)
    print("⏳ Formatting enriched context...")
    enriched_context = format_enriched_context(enriched_data)
    print(f"✓ Formatting completed: {len(enriched_context)} characters")
    
    # Add the enriched context before the end of the prompt
    # Look for specific sections where it makes sense to insert our enriched context
    split_marker = "Trả về CHÍNH XÁC dưới dạng JSON" 
    
    if split_marker in original_prompt:
        parts = original_prompt.split(split_marker, 1)
        enhanced_prompt = (
            parts[0] + 
            "\n\n" + enriched_context + "\n\n" + 
            split_marker + 
            parts[1]
        )
    else:
        # Default to adding at the end if we can't find a good split point
        enhanced_prompt = original_prompt + "\n\n" + enriched_context
    
    return enhanced_prompt

class LLMOrchestrationResult:
    """
    Lớp wrapper để định dạng kết quả từ orchestrator thành JSON
    phù hợp để trả về qua WebSocket
    """
    
    def __init__(self, result_type: str, result_data: Dict[str, Any]):
        self.result_type = result_type
        self.result_data = result_data
        self.timestamp = datetime.utcnow().isoformat()
    
    def to_websocket_json(self) -> str:
        """Convert to JSON for websocket response"""
        
        if self.result_type == "question":
            # Return formatted question JSON
            return json.dumps({
                "text": f"🤖 Kỹ sư Trưởng: {self.result_data.get('main_question', '')}",
                "type": "question",
                "timestamp": self.timestamp,
                "clarifying_questions_text": self._format_clarifying_questions(),
                "main_question": self.result_data.get("main_question", ""),
                "clarifying_questions": self.result_data.get("clarifying_questions", [])
            })
        
        elif self.result_type == "conclusion":
            # Return formatted conclusion JSON
            return json.dumps({
                "text": f"📋 Kỹ sư Trưởng: Dựa trên thông tin của bạn, tôi đã tổng hợp được các yêu cầu kỹ thuật như sau:",
                "type": "conclusion",
                "timestamp": self.timestamp,
                "technical_summary": self.result_data.get("updated_summary", ""),
                "technical_analysis": self.result_data.get("technical_analysis", {}),
                "is_completed": True
            })
        
        else:
            # Fallback to simple JSON
            return json.dumps({
                "text": f"🤖 Kỹ sư Trưởng: {self.result_data.get('main_message', 'Có kết quả mới')}",
                "type": self.result_type,
                "timestamp": self.timestamp,
                "data": self.result_data
            })
    
    def _format_clarifying_questions(self) -> str:
        """Format clarifying questions for display"""
        questions = self.result_data.get("clarifying_questions", [])
        if not questions:
            return ""
        
        result = "🔍 **Để làm rõ chi tiết kỹ thuật, vui lòng trả lời:**"
        
        for i, q in enumerate(questions, 1):
            if isinstance(q, dict) and "question" in q:
                q_text = q["question"]
                # Thêm focus kỹ thuật nếu có
                if "technical_focus" in q and "importance" in q:
                    q_text = f"{q_text} [Focus: {q['technical_focus']}, Mức độ: {q['importance']}]"
            else:
                q_text = str(q)
            
            result += f"\n{i}. {q_text}"
        
        return result

# Import MCP context enricher
try:
    from mcp_context_enricher import generate_enriched_prompt, enrich_context_with_mcp
    USE_MCP_ENRICHMENT = True
except ImportError:
    print("Warning: MCP context enrichment not available")
    USE_MCP_ENRICHMENT = False

# Import LLM Orchestrator - reflective agent core
try:
    from llm_orchestrator import LLMOrchestrator, LLMOrchestrationResult
    USE_LLM_ORCHESTRATOR = True
except ImportError:
    print("Warning: LLM Orchestrator not available")
    USE_LLM_ORCHESTRATOR = False

# Helper function to strip code blocks from LLM responses
def strip_code_block(text):
    """
    Removes markdown code block formatting from text.
    
    Args:
        text: The text potentially containing code blocks
        
    Returns:
        String with code block markers removed
    """
    # Check if text is None
    if not text:
        return ""
        
    # Remove markdown code block formatting
    pattern = r"```(?:json)?\s*([\s\S]*?)\s*```"
    matches = re.findall(pattern, text)
    
    if matches:
        return matches[0].strip()
    
    return text.strip()

# Import từ MetaGPT
# Roles
from metagpt.roles import Role, ProductManager, Architect, ProjectManager, Engineer

# Actions
from metagpt.actions import Action, UserRequirement, WriteDesign, WriteTasks, WriteCode

# Core components
from metagpt.schema import Message
from metagpt.logs import logger
from metagpt.llm import LLM
from metagpt.memory import Memory
from metagpt.team import Team
from metagpt.context import Context

# Định nghĩa JSON schemas để tái sử dụng
TECH_SPEC_JSON_SCHEMA = '''
{
  "system_overview": {
    "objective": "Mục tiêu và scope",
    "components": ["Thành phần 1", "Thành phần 2"],
    "deployment_model": "Mô hình triển khai"
  },
  "technical_architecture": {
    "sequence_diagrams": "ASCII diagram của sequence",
    "class_diagrams": "ASCII diagram của các class chính",
    "flow_diagrams": "ASCII diagram của luồng xử lý",
    "design_patterns": ["Pattern 1", "Pattern 2"],
    "component_breakdown": ["Component 1 và mô tả chi tiết", "Component 2 và mô tả chi tiết"]
  },
  "tech_stack": {
    "frameworks": ["Framework 1", "Framework 2"],
    "libraries": ["Library 1", "Library 2"],
    "tools": ["Tool 1", "Tool 2"],
    "dependencies": ["Dependency 1", "Dependency 2"]
  },
  "api_specification": [
    {
      "endpoint": "/api/resource",
      "method": "POST",
      "request_body": { "field1": "type1", "field2": "type2" },
      "response_body": { "field1": "type1", "field2": "type2" },
      "authentication": "Phương thức xác thực",
      "error_handling": "Chi tiết xử lý lỗi"
    }
  ],
  "data_model": {
    "entity_relationship": "ASCII diagram của ERD",
    "schema_definitions": [
      {
        "entity": "Entity1",
        "fields": [
          { "name": "field1", "type": "type1", "constraints": "constraints1" },
          { "name": "field2", "type": "type2", "constraints": "constraints2" }
        ],
        "relationships": ["Relationship 1", "Relationship 2"]
      }
    ],
    "indices": ["Index 1", "Index 2"],
    "migration_strategy": "Chi tiết migration strategy"
  },
  "implementation_details": {
    "functions": [
      {
        "name": "function_name",
        "signature": "def function_name(param1: type1, param2: type2) -> return_type",
        "module_path": "path.to.module",
        "pseudocode": "Chi tiết pseudocode",
        "algorithm": ["Step 1", "Step 2", "Step 3"],
        "edge_cases": ["Edge case 1", "Edge case 2"]
      }
    ],
    "directory_structure": ["folder1/", "folder1/file1.py", "folder2/"],
    "coding_standards": ["Standard 1", "Standard 2"]
  },
  "testing_strategy": {
    "unit_tests": "Chi tiết approach",
    "integration_tests": "Chi tiết approach",
    "mocks_fixtures": ["Mock 1", "Fixture 1"],
    "ci_cd": "Chi tiết pipeline"
  },
  "deployment": {
    "infrastructure": "Chi tiết setup",
    "containerization": "Chi tiết Docker/K8s",
    "monitoring_logging": "Chi tiết tools",
    "scaling": "Chi tiết strategy"
  }
}
'''

TASK_JSON_SCHEMA = '''
{
  "tasks": [
    {
      "id": 1,
      "title": "Tên function/method cụ thể với namespace đầy đủ",
      "description": "Mô tả chi tiết về nhiệm vụ của function này, các edge cases phải xử lý, và logic nghiệp vụ chi tiết",
      "priority": "high/medium/low",
      "estimated_hours": 2.5,
      "complexity": "high/medium/low",
      "type": "implementation/design/testing",
      "dependencies": ["ID của các task phải hoàn thành trước"],
      "required_skills": ["Kỹ năng kỹ thuật 1", "Kỹ năng kỹ thuật 2"],
      "function_details": {
        "file_path": "Đường dẫn file chứa function",
        "module_path": "module.submodule.file",
        "class_name": "Tên class chứa method này (nếu áp dụng)",
        "function_name": "Tên chính xác của function/method",
        "signature": "def function_name(param1: type, param2: type) -> return_type:",
        "full_qualified_name": "module.submodule.class.function",
        "parameters": [
          {"name": "param1", "type": "string/int/Dict[str,Any]/etc", "description": "Mô tả chi tiết parameter", "validation": "Cách validate parameter này"},
          {"name": "param2", "type": "string/int/etc", "description": "Mô tả chi tiết parameter", "default": "Giá trị default nếu có"}
        ],
        "return_value": {"type": "return_type", "description": "Mô tả chi tiết giá trị trả về", "possible_values": ["Các giá trị có thể trả về"]},
        "algorithm_steps": [
          "1. Bước 1: Chi tiết từng bước của thuật toán",
          "2. Bước 2: Chi tiết từng bước của thuật toán",
          "3. Bước 3: Chi tiết từng bước của thuật toán"
        ],
        "edge_cases": ["Edge case 1 và cách xử lý chi tiết", "Edge case 2 và cách xử lý chi tiết"],
        "exceptions": [
          {"type": "ExceptionType1", "when": "Khi nào xảy ra exception này", "handling": "Cách xử lý"},
          {"type": "ExceptionType2", "when": "Khi nào xảy ra exception này", "handling": "Cách xử lý"}
        ],
        "design_patterns": ["Pattern 1 được áp dụng", "Pattern 2 được áp dụng"],
        "testing_strategy": "Chi tiết cách test function này"
      },
      "subtasks": [
        {
          "id": "1.1",
          "title": "Validate đầu vào của function",
          "description": "Mô tả chi tiết cách validate từng parameter",
          "estimated_hours": 0
        },
        {
          "id": "1.2",
          "title": "Tiêu đề subtask (ví dụ: Xử lý core logic của function)",
          "description": "Mô tả chi tiết subtask",
          "estimated_hours": 0
        },
        {
          "id": "1.3",
          "title": "Tiêu đề subtask (ví dụ: Xử lý lỗi và edge cases)",
          "description": "Mô tả chi tiết subtask",
          "estimated_hours": 0
        }
      ]
    }
  ],
  "assignments": [
    {
      "task_id": 1,
      "member_name": "Tên thành viên",
      "assigned_hours": 0,
      "assignment_rationale": "Lý do phân công"
    }
  ],
  "development_flow": {
    "phases": [
      {
        "name": "Tên giai đoạn",
        "description": "Mô tả giai đoạn",
        "tasks": [1, 2, 3],
        "duration": "Thời gian ước tính"
      }
    ]
  },
  "team_summary": {
    "team_size": 0,
    "skill_coverage": ["Kỹ năng 1", "Kỹ năng 2"],
    "workload_distribution": "Phân bổ công việc"
  }
}
'''

ANALYSIS_RESULT_JSON_SCHEMA = '''
{
  "updated_summary": "Tổng hợp hiểu biết hiện tại về yêu cầu",
  "technical_analysis": {
    "architecture": "Kiến trúc đề xuất",
    "components": ["Component1", "Component2"],
    "data_flow": "Mô tả luồng dữ liệu",
    "class_diagram": "ASCII diagram của class diagram chính",
    "sequence_diagram": "ASCII diagram của sequence diagram chính",
    "api_endpoints": ["API1", "API2"],
    "database_schema": "Mô tả schema",
    "integrations": ["Integration1", "Integration2"]
  },
  "next_action": "ask_more hoặc sufficient",
  "next_question": "Câu hỏi kỹ thuật chi tiết về triển khai hoặc workflow",
  "clarifying_questions": [
    {
      "question": "Chi tiết kỹ thuật về triển khai của chức năng?",
      "technical_focus": "triển khai",
      "importance": "Cao" 
    },
    {
      "question": "Chi tiết cụ thể về thuật toán/công nghệ/API cần sử dụng?",
      "technical_focus": "công nghệ",
      "importance": "Trung bình"
    }
  ],
  "final_summary": "Tóm tắt cuối cùng với chi tiết kỹ thuật",
  "is_completed": true,
  "role_discussions": [
    { "role": "Quản lý Sản phẩm", "highlights": "Điểm nhấn từ Quản lý Sản phẩm" },
    { "role": "Kiến trúc sư", "highlights": "Điểm nhấn từ Kiến trúc sư" },
    { "role": "Kỹ sư", "highlights": "Điểm nhấn từ Kỹ sư" }
  ]
}
'''

app = FastAPI(title="MetaGPT Task Scheduler")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

# Global storage for sessions and results
clarification_sessions: Dict[str, Dict[str, Any]] = {}
results_store: Dict[str, Dict[str, Any]] = {}
# Storage for tasks and requirements for unit test generation
project_data_storage: Dict[str, Dict[str, Any]] = {}

@app.get("/")
async def serve_ui():
    return FileResponse("static/index.html")

class TeamMember(BaseModel):
    name: str
    role: Optional[str] = None
    experience: Optional[int] = 0
    strengths: List[str] = Field(default_factory=list)

class ClarificationInitRequest(BaseModel):
    session_id: str
    general_requirement: str
    team_data: List[TeamMember]

@app.post("/init")
async def init_clarification(req: ClarificationInitRequest):
    clarification_sessions[req.session_id] = {
        "requirement": req.general_requirement,
        "team_data": [m.dict() for m in req.team_data],
        "chat_history": []
    }
    return {"status": "initialized", "session_id": req.session_id}

@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await websocket.accept()
    if session_id not in clarification_sessions:
        await websocket.close(code=1008)
        return
    
    try:
        session = clarification_sessions[session_id]
        
        # Sử dụng LLM Orchestrator nếu có sẵn, nếu không thì sử dụng cách cũ
        if USE_LLM_ORCHESTRATOR:
            from llm_orchestrator import LLMOrchestrator, LLMOrchestrationResult, LLMAction
            
            # Khởi tạo orchestrator
            llm_orchestrator = LLMOrchestrator()
            session["orchestrator"] = llm_orchestrator
            
            # Gửi tin nhắn chào
            welcome_json = json.dumps({
                "text": "🤖 Kỹ sư Trưởng: Chào bạn! Tôi sẽ trao đổi với bạn để hiểu rõ yêu cầu kỹ thuật của dự án.",
                "type": "welcome", 
                "timestamp": datetime.utcnow().isoformat()
            })
            await websocket.send_text(welcome_json)
            await asyncio.sleep(1)
            
            # Khởi tạo phiên với yêu cầu ban đầu
            await llm_orchestrator.initialize(session["requirement"], session["team_data"])
            
            # Sinh câu hỏi đầu tiên
            first_question = await llm_orchestrator.generate_first_question()
            
            # Lưu câu hỏi để tham chiếu sau này
            session["last_question"] = first_question.get("main_question", "")
            
            # Tạo kết quả orchestration và gửi đi
            first_result = LLMOrchestrationResult("question", first_question)
            await websocket.send_text(first_result.to_websocket_json())
            
            # Vòng lặp xử lý chat
            while True:
                user_message = await websocket.receive_text()
                
                # Kiểm tra nếu người dùng muốn kết thúc
                if user_message.lower().strip() in ["done", "xong", "ok", "confirm", "xác nhận"]:
                    if session.get("ready_to_confirm") or llm_orchestrator.session_data.get("ready_to_conclude"):
                        # Hoàn thiện requirements
                        final_requirements = await llm_orchestrator.finalize_requirements()
                        session["final_requirements"] = final_requirements
                        
                        confirm_json = json.dumps({
                            "text": "✅ Cảm ơn bạn! Phần clarification đã hoàn thành.",
                            "type": "confirmation", 
                            "timestamp": datetime.utcnow().isoformat()
                        })
                        await websocket.send_text(confirm_json)
                        break
                    else:
                        clarify_json = json.dumps({
                            "text": "🤔 Tôi cần thêm một chút thông tin nữa để hiểu rõ yêu cầu. Hãy tiếp tục trả lời nhé!",
                            "type": "clarify", 
                            "timestamp": datetime.utcnow().isoformat()
                        })
                        await websocket.send_text(clarify_json)
                        continue
                
                # Nếu người dùng đã xác nhận nhưng muốn bổ sung thêm
                if session.get("ready_to_confirm") or llm_orchestrator.session_data.get("ready_to_conclude"):
                    # Xử lý yêu cầu bổ sung
                    process_result = await llm_orchestrator.process_user_answer(
                        user_message, 
                        "Bạn có muốn bổ sung thêm requirements nào không?"
                    )
                    
                    # Cập nhật trạng thái và gửi kết quả
                    if process_result["action"] == "conclude":
                        # Người dùng đã hoàn thành việc bổ sung
                        session["final_requirements"] = process_result.get("updated_summary", "")
                        session["ready_to_confirm"] = True
                        
                        result = LLMOrchestrationResult("conclusion", process_result)
                        await websocket.send_text(result.to_websocket_json())
                    else:
                        # Người dùng tiếp tục bổ sung, gửi câu hỏi tiếp theo
                        session["last_question"] = process_result.get("main_question", "")
                        
                        result = LLMOrchestrationResult("question", process_result)
                        await websocket.send_text(result.to_websocket_json())
                    
                    continue
                
                # Xử lý câu trả lời thông thường
                last_question = session.get("last_question", "")
                process_result = await llm_orchestrator.process_user_answer(user_message, last_question)
                
                # Cập nhật trạng thái session
                session["last_question"] = process_result.get("main_question", "")
                
                if process_result["action"] == "conclude":
                    # Người dùng đã cung cấp đủ thông tin
                    session["ready_to_confirm"] = True
                    session["final_requirements"] = process_result.get("updated_summary", "")
                    
                    result = LLMOrchestrationResult("conclusion", process_result)
                    await websocket.send_text(result.to_websocket_json())
                    
                    # Gửi thêm thông báo về việc hoàn thành
                    finalize_json = json.dumps({
                        "text": "📋 Tôi đã ghi nhận đầy đủ yêu cầu kỹ thuật. Bạn có muốn bổ sung thêm gì không trước khi tiếp tục?",
                        "type": "ready_to_confirm", 
                        "timestamp": datetime.utcnow().isoformat()
                    })
                    await asyncio.sleep(1)
                    await websocket.send_text(finalize_json)
                else:
                    # Tiếp tục hỏi
                    result = LLMOrchestrationResult("question", process_result)
                    await websocket.send_text(result.to_websocket_json())
                    
                    # Lưu trạng thái
                    session["current_summary"] = process_result.get("updated_summary", "")
        else:
            # === Phương pháp cũ nếu không có Orchestrator ===
            # Khởi tạo role Kỹ sư Trưởng từ MetaGPT (sử dụng role Engineer)
            lead_engineer_role = Engineer()
            
            # Tạo context để chia sẻ dữ liệu
            team_context = Context()
            # Gán dữ liệu vào thuộc tính kwargs của Context
            team_context.kwargs.set("requirement", session["requirement"])
            team_context.kwargs.set("team_data", session["team_data"])
            
            # Khởi tạo memory cho role
            lead_engineer_memory = Memory()
            
            # Thiết lập role với context và memory
            lead_engineer_role.context = team_context
            lead_engineer_role.memory = lead_engineer_memory
            
            # Lưu role vào session để sử dụng sau
            session["roles"] = {
                "lead_engineer": lead_engineer_role
            }
            session["team_context"] = team_context
            
            # --- MCP: Phân tích cấu trúc dự án trước khi role ảo bắt đầu ---
            try:
                # Kiểm tra xem mcp_client hoặc mcp_optimizer đã được import chưa
                try:
                    # Thử import từ mcp_optimizer (cách mới)
                    from mcp_optimizer import analyze_requirement_with_mcp_auto_chaining
                    
                    # Sử dụng hàm analyze_requirement_with_mcp_auto_chaining để lấy thông tin dự án
                    mcp_analysis = await analyze_requirement_with_mcp_auto_chaining(
                    requirement=session["requirement"],
                    chat_history=[]
                )
                    
                except (ImportError, ModuleNotFoundError):
                    # Fallback: Thử import từ mcp_client (cách cũ)
                    try:
                        from mcp_client import analyze_requirement_with_mcp_reasoning
                        mcp_analysis = await analyze_requirement_with_mcp_reasoning(
                            requirement=session["requirement"],
                            chat_history=[]
                        )
                    except (ImportError, ModuleNotFoundError):
                        # Fallback cuối cùng: Sử dụng hàm fallback
                        mcp_analysis = await fallback_mcp_analysis(session["requirement"])
                
                session["mcp_analysis"] = mcp_analysis
                # Đưa thông tin này vào context để role ảo "hiểu" cấu trúc dự án
                team_context.kwargs.set("mcp_analysis", mcp_analysis)
            except Exception as e:
                logger.error(f"MCP server error: {e}")
                session["mcp_analysis_error"] = str(e)
            
            # Bước 1: Khởi tạo conversation nếu chưa có
            if not session.get("conversation_started"):
                # Gửi tin nhắn chào dưới dạng JSON với loại là welcome
                welcome_json = json.dumps({
                    "text": "🤖 Kỹ sư Trưởng: Chào bạn! Tôi sẽ trao đổi với bạn để hiểu rõ yêu cầu kỹ thuật của dự án.",
                    "type": "welcome", 
                    "timestamp": datetime.utcnow().isoformat()
                })
                await websocket.send_text(welcome_json)
                await asyncio.sleep(1)
                
                # Tạo action cho việc tạo câu hỏi ban đầu
                class GenerateInitialQuestionsAction(Action):
                    """Action để tạo câu hỏi ban đầu về requirements"""
                    
                    async def run(self):
                        # Gọi LLM để sinh câu hỏi đầu tiên và các câu hỏi làm rõ với focus vào chi tiết kỹ thuật phát triển
                        llm_prompt = f"""
Bạn là Kỹ sư Trưởng. Hãy đọc yêu cầu sau và sinh ra 1 câu hỏi chính để làm rõ về các chi tiết kỹ thuật, kèm 2-3 câu hỏi phụ nếu cần.

YÊU CẦU: {session['requirement']}
THÔNG TIN TEAM: {session['team_data']}

HƯỚNG DẪN CHI TIẾT:
1. Tập trung vào các khía cạnh TRIỂN KHAI KỸ THUẬT và CHI TIẾT PHÁT TRIỂN cụ thể
2. Hỏi về stack công nghệ, kiến trúc, database, API, integration, workflow kỹ thuật...
3. Hỏi chi tiết đến mức các class/function quan trọng nếu có thể
4. TRÁNH hỏi về các giới hạn (limit) hoặc thời gian xử lý tối đa
5. Hỏi về các requirement kỹ thuật chi tiết mà developer cần để triển khai

**BẮT BUỘC**: Chỉ được trả lời bằng tiếng Việt, không sử dụng tiếng Anh, không giải thích thêm, không markdown/code block, không thêm bất kỳ ký tự nào ngoài JSON.

Trả về JSON:
{{
  "main_question": "...",
  "clarifying_questions": ["...", "..."]
}}
"""
                initial_question_action = GenerateInitialQuestionsAction()
                llm_result = await initial_question_action.run()
                
                # Handle potential None result from LLM
                if llm_result is None:
                    logger.warning(f"LLM returned None for initial questions in session {session_id}")
                    first_question_data = {
                        "main_question": "Bạn có thể chia sẻ thêm về các yêu cầu kỹ thuật chi tiết của dự án không?",
                        "clarifying_questions": [
                            "Bạn đang sử dụng stack công nghệ nào?", 
                            "Có yêu cầu đặc biệt về hiệu năng hoặc kiến trúc không?"
                        ]
                    }
                else:
                    try:
                        first_question_data = json.loads(strip_code_block(llm_result))
                    except json.JSONDecodeError:
                        logger.error(f"Failed to parse JSON from LLM result: {llm_result}")
                        first_question_data = {
                            "main_question": "Bạn có thể chia sẻ thêm về các yêu cầu kỹ thuật chi tiết của dự án không?",
                            "clarifying_questions": [
                                "Bạn đang sử dụng stack công nghệ nào?", 
                                "Có yêu cầu đặc biệt về hiệu năng hoặc kiến trúc không?"
                            ]
                        }
                
                # Lưu kết quả vào memory của PM role
                session["roles"]["lead_engineer"].memory.add(Message(role="assistant", content=json.dumps(first_question_data)))
                
                session["conversation_started"] = True
                session["current_summary"] = ""
                
                # Kiểm tra xem kết quả là JSON hay string
                if isinstance(first_question_data, dict):
                    main_question = first_question_data.get("main_question", "")
                    clarifying_questions = first_question_data.get("clarifying_questions", [])
                    
                    # Lưu câu hỏi chính
                    session["last_question"] = main_question
                    
                    # Lưu các câu hỏi làm rõ để sử dụng sau
                    session["clarifying_questions"] = clarifying_questions
                    
                    # Chuẩn bị và gửi câu hỏi chính dưới dạng JSON với loại là question
                    # Gộp main question và clarifying questions thành một message có định dạng tốt hơn
                    combined_question_text = f"🤖 Kỹ sư Trưởng: {main_question}"
                    
                    clarifying_text = ""
                    if clarifying_questions:
                        clarifying_text = "🔍 **Để làm rõ chi tiết kỹ thuật, vui lòng trả lời:**"
                        for i, q_obj in enumerate(clarifying_questions, 1):
                            # Kiểm tra xem q_obj có phải là dictionary không
                            if isinstance(q_obj, dict) and "question" in q_obj:
                                q_text = q_obj["question"]
                                # Thêm focus kỹ thuật nếu có, sử dụng cách an toàn với format() thay vì f-string
                                if "technical_focus" in q_obj and "importance" in q_obj:
                                    focus = q_obj.get("technical_focus", "")
                                    importance = q_obj.get("importance", "")
                                    q_text = "{} [Focus: {}, Mức độ: {}]".format(q_text, focus, importance)
                            else:
                                # Nếu là string đơn giản thì sử dụng luôn
                                q_text = str(q_obj)
                            clarifying_text += "\n{}. {}".format(i, q_text)
                    
                    # Tạo JSON có cấu trúc rõ ràng hơn
                    main_question_json = json.dumps({
                        "text": combined_question_text,
                        "type": "question", 
                        "timestamp": datetime.utcnow().isoformat(),
                        "clarifying_questions_text": clarifying_text,
                        "main_question": main_question,
                        "clarifying_questions": clarifying_questions
                    })
                    await websocket.send_text(main_question_json)
                else:
                    # Trực tiếp parse first_question_data
                    session["last_question"] = first_question_data
            
            # Vòng lặp chat từng bước
            while True:
                user_message = await websocket.receive_text()
                
                # Kiểm tra nếu người dùng muốn kết thúc
                if user_message.lower().strip() in ["done", "xong", "ok", "confirm", "xác nhận"]:
                    if session.get("ready_to_confirm"):
                        confirm_json = json.dumps({
                            "text": "✅ Cảm ơn bạn! Phần clarification đã hoàn thành.",
                            "type": "confirmation", 
                            "timestamp": datetime.utcnow().isoformat()
                        })
                        await websocket.send_text(confirm_json)
                        break
                    else:
                        clarify_json = json.dumps({
                            "text": "🤔 Tôi cần thêm một chút thông tin nữa để hiểu rõ yêu cầu. Hãy tiếp tục trả lời nhé!",
                            "type": "clarify", 
                            "timestamp": datetime.utcnow().isoformat()
                        })
                        await websocket.send_text(clarify_json)
                        continue
                
                # Xử lý đặc biệt khi đã "sufficient" nhưng người dùng muốn bổ sung thêm
                if session.get("ready_to_confirm"):
                    # Lưu câu trả lời bổ sung vào chat history
                    session["chat_history"].append({
                        "question": "Bạn có muốn bổ sung thêm requirements nào không?", 
                        "answer": user_message,
                        "timestamp": datetime.utcnow().isoformat()
                    })
                    
                    # Người dùng đang bổ sung thêm
                    # Lưu yêu cầu bổ sung
                    current_summary = session.get("current_summary", "")
                    updated_summary = f"{current_summary}\n\n**Bổ sung thêm:**\n- {user_message}"
                    session["current_summary"] = updated_summary
                    session["final_requirements"] = updated_summary
                    
                    # Phân tích yêu cầu bổ sung để làm rõ
                    class AnalyzeSuppRequirementAction(Action):
                        """Phân tích yêu cầu bổ sung và tạo các câu hỏi làm rõ"""
                        
                        async def run(self, requirement, new_requirement):
                            prompt = f"""
Bạn là Lead Technical Architect và Senior Software Engineer. Nhiệm vụ: phân tích yêu cầu bổ sung từ góc độ kỹ thuật chi tiết và tạo các câu hỏi để làm rõ các chi tiết TRIỂN KHAI.

REQUIREMENT GỐC:
{requirement}

YÊU CẦU BỔ SUNG MỚI:
{new_requirement}

Hãy phân tích yêu cầu bổ sung từ góc độ KỸ THUẬT TRIỂN KHAI và:
1. Xác định các điểm chưa rõ về mặt triển khai kỹ thuật (classes, functions, API, database schemas...)
2. Tạo 2-3 câu hỏi CHI TIẾT về mặt KỸ THUẬT (không hỏi về limits/constraints)
3. Đề xuất cách tích hợp kỹ thuật cụ thể (structure, classes, interfaces...)
4. Vẽ sơ đồ tuần tự hoặc biểu đồ luồng xử lý bằng ASCII nếu có thể

Trả về CHÍNH XÁC dưới dạng JSON không có markdown code block (KHÔNG THÊM 
json hay
):
{{
    "summary": "Tóm tắt hiểu biết về yêu cầu bổ sung từ góc độ kỹ thuật",
    "technical_details": {{
        "classes": ["Class1", "Class2"],
        "apis": ["API1", "API2"],
        "database": "Cấu trúc database và schemas"
    }},
    "clarifying_questions": [
        "Câu hỏi kỹ thuật 1",
        "Câu hỏi kỹ thuật 2",
        "Câu hỏi kỹ thuật 3"
    ],
    "integration_suggestion": "Đề xuất cách tích hợp kỹ thuật",
    "flow_diagram": "ASCII diagram nếu có thể"
}}
"""
                            return await LLM().aask(prompt)
                    
                    try:
                        # Phân tích yêu cầu bổ sung
                        analyzer = AnalyzeSuppRequirementAction()
                        analysis_result_str = await analyzer.run(current_summary, user_message)
                        
                        # Xử lý kết quả
                        analysis_result = {}
                        try:
                            # Parse JSON
                            import re
                            # Thử parse trực tiếp
                            analysis_result = json.loads(strip_code_block(analysis_result_str))
                        except Exception as e:
                            logger.error(f"Lỗi khi parse JSON: {e}")
                            # Parse từ LLM response
                            analysis_result = {}
                            
                            # Cố gắng tạo structure cho analysis_result từ response
                            if "summary" in analysis_result_str.lower():
                                analysis_result["summary"] = f"Đã ghi nhận yêu cầu bổ sung: {user_message}"
                            if "question" in analysis_result_str.lower():
                                analysis_result["clarifying_questions"] = []
                            if "integrat" in analysis_result_str.lower():
                                analysis_result["integration_suggestion"] = "Tích hợp vào hệ thống"
                        
                        # Tạo tin nhắn tổng hợp
                        combined_message = f"""📝 **BA AI**: Tôi ghi nhận thêm requirement: '{user_message}'

🔍 **Phân tích yêu cầu bổ sung:**
{analysis_result.get('summary', 'Đã ghi nhận yêu cầu bổ sung')}

❓ **Một số câu hỏi để làm rõ thêm:**
"""
                        
                        # Thêm chi tiết kỹ thuật nếu có
                        if analysis_result.get('technical_details'):
                            tech_details = analysis_result.get('technical_details', {})
                            combined_message += "\n\n🔧 **Chi tiết kỹ thuật:**\n"
                            
                            if tech_details.get('classes'):
                                combined_message += f"- **Classes/Components:** {', '.join(tech_details.get('classes'))}\n"
                            
                            if tech_details.get('apis'):
                                combined_message += f"- **APIs:** {', '.join(tech_details.get('apis'))}\n"
                            
                            if tech_details.get('database'):
                                combined_message += f"- **Database:** {tech_details.get('database')}\n"
                        
                        # Thêm các câu hỏi làm rõ
                        combined_message += "\n\n❓ **Câu hỏi kỹ thuật chi tiết:**\n"
                        for i, q in enumerate(analysis_result.get('clarifying_questions', []), 1):
                            combined_message += f"{i}. {q}\n"
                        
                        # Thêm đề xuất tích hợp
                        combined_message += f"""
🔄 **Đề xuất tích hợp kỹ thuật:** 
{analysis_result.get('integration_suggestion', 'Sẽ được tích hợp vào hệ thống')}
"""
                        
                        # Thêm flow diagram nếu có
                        if analysis_result.get('flow_diagram'):
                            combined_message += f"""
📊 **Sơ đồ luồng xử lý:**
{analysis_result.get('flow_diagram')}

"""
                        
                        combined_message += f"""
📋 **Requirements kỹ thuật đã cập nhật chi tiết:**
{updated_summary}

📊 **Task breakdown đến mức function:**
- Đã phân chia các tasks chi tiết đến mức function
- Mô tả input/output của từng function
- Sắp xếp theo thứ tự triển khai hợp lý
- Xác định dependencies giữa các tasks

✅ Vui lòng trả lời các câu hỏi làm rõ hoặc bổ sung thêm yêu cầu kỹ thuật chi tiết. Bấm 'Xác nhận & Tiếp tục' khi bạn đã hài lòng!"""

                    except Exception as e:
                        logger.error(f"Lỗi khi phân tích yêu cầu bổ sung: {e}")
                        # Tiếp tục với yêu cầu đã cập nhật
                        combined_message = f"""📝 **Technical Analyst**: Tôi ghi nhận thêm yêu cầu kỹ thuật: '{user_message}'

📋 **Requirements kỹ thuật đã cập nhật:**
{updated_summary}

📊 **Task breakdown sẽ được cập nhật chi tiết đến mức function/method**

✅ Vui lòng cho biết nếu bạn cần điều chỉnh hay bổ sung yêu cầu kỹ thuật. Nhấn 'Xác nhận & Tiếp tục' khi đã sẵn sàng!"""

                    # Gửi tin nhắn tổng hợp
                    await websocket.send_text(combined_message)
                    
                    continue  # Quay lại đầu vòng lặp để chờ input tiếp theo
                
                # Lưu câu trả lời bình thường
                if session.get("last_question"):
                    session["chat_history"].append({
                        "question": session["last_question"], 
                        "answer": user_message,
                        "timestamp": datetime.utcnow().isoformat()
                    })
                    
                    # Lưu câu trả lời vào memory của Kỹ sư Trưởng
                    user_message_obj = Message(role="user", content=user_message)
                    session["roles"]["lead_engineer"].memory.add(user_message_obj)
                    
                    # Cập nhật context chung
                    session["team_context"].kwargs.set(f"user_answer_{len(session['chat_history'])}", user_message)
                
                # Gửi thông báo đang xử lý cho người dùng
                processing_msg = json.dumps({
                    "text": "⏳ Đang phân tích câu trả lời của bạn...",
                    "type": "processing", 
                    "timestamp": datetime.utcnow().isoformat()
                })
                await websocket.send_text(processing_msg)
            
    except Exception as e:
        logger.exception(f"WebSocket error: {str(e)}")
        await websocket.close(code=1011)


# Tạo action cho việc phân tích yêu cầu với nhiều roles thảo luận
class AnalyzeRequirementsAction(Action):
                """Action để phân tích yêu cầu với một role duy nhất - Kỹ sư Trưởng"""
                
                async def run(self, requirement, chat_history, team_data):
                    """Action để phân tích yêu cầu với một role duy nhất - Kỹ sư Trưởng"""
                    
                    try:
                        # --- Thử sử dụng context enrichment từ mcp_context_enricher nếu có ---
                        enriched_context = ""
                        if USE_MCP_ENRICHMENT and chat_history and len(chat_history) > 0:
                            try:
                                # Lấy câu trả lời mới nhất của người dùng
                                latest_answer = chat_history[-1].get("answer", "")
                                
                                # Làm giàu context với MCP tools
                                print(f"Làm giàu context với câu trả lời mới nhất: '{latest_answer[:50]}...'")
                                enriched_data = await enrich_context_with_mcp(latest_answer, chat_history)
                                
                                # Format enriched context
                                from mcp_context_enricher import format_enriched_context
                                enriched_context = format_enriched_context(enriched_data)  # Not using await anymore
                                print("Đã làm giàu context thành công")
                            except Exception as e:
                                print(f"Lỗi khi làm giàu context: {e}")
                        
                        # Khởi tạo mcp_analysis với fallback
                        mcp_analysis = await fallback_mcp_analysis(requirement, chat_history)
                        
                        # Basic project info
                        project_info = "Basic project info"
                        if mcp_analysis.get("project_overview"):
                            project_info = f"Project info: {mcp_analysis['project_overview']}"
                        
                        # Simple prompt for testing
                        original_prompt = f"""
Phân tích yêu cầu:
{requirement}

Thông tin dự án:
{project_info}

{enriched_context}
"""
                        
                        # Enhanced prompt if available
                        enhanced_prompt = original_prompt
                        if USE_MCP_ENRICHMENT and chat_history and len(chat_history) > 0:
                            try:
                                latest_answer = chat_history[-1].get("answer", "")
                                enhanced_prompt = await generate_enriched_prompt(
                                    original_prompt=original_prompt,
                                    user_answer=latest_answer, 
                                    chat_history=chat_history
                                )
                                print("Enhanced prompt created")
                            except Exception as e:
                                print(f"Error creating enhanced prompt: {e}")
                        
                        # Get analysis from LLM
                        final_analysis = await LLM().aask(enhanced_prompt)
                        return final_analysis
                        
                    except Exception as e:
                        logger.error(f"Error in AnalyzeRequirementsAction.run: {e}")
                        return json.dumps({
                            "error": str(e),
                            "next_action": "ask_more",
                            "is_completed": False
                        })
            # End of AnalyzeRequirementsAction class

async def process_with_metagpt(session_id: str, requirement: str, team_data: List[Dict[str, Any]]):
    """
    Xử lý yêu cầu với MetaGPT team
    
    Args:
        session_id: ID của phiên làm rõ yêu cầu
        requirement: Yêu cầu đã làm rõ
        team_data: Thông tin về team nếu có
    """
    try:
        logger.info(f"Bắt đầu xử lý MetaGPT cho session {session_id}")
        results_store[session_id] = {
            "status": "processing", 
            "start_time": datetime.utcnow().isoformat()
        }

        # Khởi tạo context chung
        team_context = Context()
        team_context.kwargs.set("requirement", requirement)
        team_context.kwargs.set("team_data", team_data)
        
        # Khởi tạo team với các roles
        team_members = []
        
        # Tạo Product Manager role
        pm = ProductManager()
        pm.set_context(team_context)
        team_members.append(pm)
        
        # Tạo Architect role
        architect = Architect()
        architect.set_context(team_context)
        team_members.append(architect)
        
        # Tạo Project Manager role
        project_manager = ProjectManager()
        project_manager.set_context(team_context)
        team_members.append(project_manager)
        
        # Tạo Engineer role
        engineer = Engineer()
        engineer.set_context(team_context)
        team_members.append(engineer)
        
        # Khởi tạo team
        team = Team(roles=team_members)
        
        # Kích hoạt team với tin nhắn yêu cầu
        message = Message(content=requirement, role="user")
        result = await team.run(message)
        
        # Lưu kết quả
        result_json = {
            "status": "completed",
            "end_time": datetime.utcnow().isoformat(),
            "design": result.get("design", ""),
            "tasks": result.get("tasks", []),
            "requirements": requirement,
            "team_members": team_data,
            "timestamp": datetime.utcnow().isoformat(),
            "status": "ready_for_unittest_generation"
        }
        results_store[session_id] = result_json
        logger.info(f"Successfully completed MetaGPT processing for session {session_id}")
    except Exception as e:
        logger.exception(f"Failed MetaGPT processing for session {session_id}: {e}")
        results_store[session_id] = {
            "status": "failed",
            "error": f"Processing error: {str(e)}"
        }
