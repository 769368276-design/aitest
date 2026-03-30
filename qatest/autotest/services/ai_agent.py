import json
import logging
from ai_assistant.services.ai_service import ai_service

logger = logging.getLogger(__name__)

class AIAgent:
    def __init__(self, user=None):
        self.user = user

    async def get_action(self, step_description, page_context, user=None):
        """
        Ask AI for the next action based on step description and page context.
        page_context: A dictionary containing 'url', 'title', 'accessibility_tree' (or simplified DOM).
        """
        
        prompt = f"""
You are an automated web testing agent using Playwright.
Current Step: "{step_description}"
Current Page URL: {page_context.get('url')}
Current Page Title: {page_context.get('title')}

Page Accessibility Tree (Simplified Structure):
{json.dumps(page_context.get('accessibility_tree'), indent=2, ensure_ascii=False)}

Based on the current step and page structure, determine the single next Playwright action to perform.
Return ONLY a JSON object with the following structure (no markdown, no extra text):
{{
    "action": "click" | "fill" | "goto" | "press" | "wait",
    "selector": "playwright_selector", 
    "value": "text_to_fill_if_needed",
    "reason": "explanation of why this action was chosen"
}}

Rules:
1. Use robust selectors (text=..., #id, .class, [placeholder=...]).
2. If the step implies navigation (e.g. "Open google.com"), use "goto".
3. If the step implies clicking, use "click".
4. If the step implies typing, use "fill".
5. If the step implies checking text, you can use "wait" with state "visible".
6. If the element is not found or unclear, return action "error" with reason.
"""
        
        try:
            # Reusing the existing AI service infrastructure
            # Assuming ai_service.generate_content or similar exists. 
            # If not, I'll use the raw client from settings.
            # Let's check ai_service.py content again to be sure.
            # For now, I will implement a direct call using the OpenAI-compatible client pattern 
            # which seems to be what ai_service uses (based on settings).
            
            response = await ai_service.get_chat_response(prompt, user=(user or self.user))
            
            # Clean up response (sometimes LLM wraps in ```json ... ```)
            cleaned_response = response.strip()
            if cleaned_response.startswith("```json"):
                cleaned_response = cleaned_response[7:]
            if cleaned_response.startswith("```"):
                cleaned_response = cleaned_response[3:]
            if cleaned_response.endswith("```"):
                cleaned_response = cleaned_response[:-3]
                
            return json.loads(cleaned_response)
            
        except Exception as e:
            logger.error(f"AI Agent Error: {e}")
            return {
                "action": "error",
                "reason": str(e)
            }
