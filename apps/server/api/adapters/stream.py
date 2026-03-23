import json
import uuid
import logging
from typing import AsyncGenerator, Dict, Any, List, Optional

logger = logging.getLogger("ghostllm.adapters.stream")

class StreamNormalizer:
    """
    Stateful SSE normalizer for Anthropic protocol.
    Ensures message_start, content_block_start, and message_stop events 
    are precisely sequenced to satisfy Claude Code's internal state machine.
    """

    def __init__(self, requested_model: str):
        self.requested_model = requested_model
        self.message_id = f"msg_{uuid.uuid4().hex}"
        self.thinking_started = False
        self.text_started = False
        self.active_indices = {}
        self.finish_reason = "end_turn"

    async def generate(self, openai_stream: AsyncGenerator[str, None]) -> AsyncGenerator[str, None]:
        # 1. Message Start
        yield self._event("message_start", {
            "message": {
                "id": self.message_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": self.requested_model
            }
        })

        async for chunk in openai_stream:
            if not chunk.startswith("data: "):
                continue
                
            data_str = chunk[6:].strip()
            if data_str == "[DONE]":
                break
                
            try:
                data = json.loads(data_str)
                if not data.get("choices"):
                    continue
                    
                delta = data["choices"][0].get("delta", {})
                
                # A. Handle Reasoning (Thinking) Block
                reasoning = delta.get("reasoning_content", "") or delta.get("reasoning", "")
                if reasoning:
                    if not self.thinking_started:
                        # Anthropic expects thinking block at early index
                        yield self._event("content_block_start", {
                            "index": 0,
                            "content_block": {"type": "thinking", "thinking": "", "signature": "GhostLLM-k2.5"}
                        })
                        self.active_indices[0] = "thinking"
                        self.thinking_started = True
                    
                    yield self._event("content_block_delta", {
                        "index": 0,
                        "delta": {"type": "thinking_delta", "thinking": reasoning}
                    })

                # B. Handle Text Content Block
                content = delta.get("content", "")
                if content:
                    # If we have text, we should close thinking if it was open? 
                    # Actually Anthropic allows multiple blocks. We'll use index 1 for text.
                    text_idx = 0 if not self.thinking_started else 1
                    
                    if not self.text_started:
                        if self.thinking_started:
                            # Close the thinking block if we start text? 
                            # Anthropic usually stops the block before starting next.
                            yield self._event("content_block_stop", {"index": 0})
                        
                        yield self._event("content_block_start", {
                            "index": text_idx,
                            "content_block": {"type": "text", "text": ""}
                        })
                        self.active_indices[text_idx] = "text"
                        self.text_started = True
                    
                    yield self._event("content_block_delta", {
                        "index": text_idx,
                        "delta": {"type": "text_delta", "text": content}
                    })

                # B. Handle Tool Calls
                tool_calls = delta.get("tool_calls")
                if tool_calls:
                    for tc in tool_calls:
                        tc_index = tc.get("index", 0) + 1 # Offset by 1 for text block
                        fn = tc.get("function", {})
                        
                        if tc_index not in self.active_indices:
                            self.active_indices[tc_index] = "tool_use"
                            call_id = tc.get("id") or f"call_{uuid.uuid4().hex}"
                            yield self._event("content_block_start", {
                                "index": tc_index,
                                "content_block": {
                                    "type": "tool_use",
                                    "id": call_id,
                                    "name": fn.get("name", "unknown"),
                                    "input": {}
                                }
                            })
                            
                            if fn.get("arguments"):
                                yield self._event("content_block_delta", {
                                    "index": tc_index,
                                    "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]}
                                })
                        elif "arguments" in fn:
                            yield self._event("content_block_delta", {
                                "index": tc_index,
                                "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]}
                            })

                # C. Capture Finish Reason
                freason = data["choices"][0].get("finish_reason")
                if freason:
                    if freason == "tool_calls":
                        self.finish_reason = "tool_use"
                    elif freason == "length":
                        self.finish_reason = "max_tokens"
                    else:
                        self.finish_reason = "end_turn"

            except Exception as e:
                logger.error(f"Stream Normalization Error: {e}")
                continue

        # 2. Final Sequence
        for idx in sorted(self.active_indices.keys(), reverse=True):
            yield self._event("content_block_stop", {"index": idx})
            
        yield self._event("message_delta", {
            "delta": {"stop_reason": self.finish_reason, "stop_sequence": None}
        })
        yield self._event("message_stop", {})

    def _event(self, type: str, data: Dict[str, Any]) -> str:
        payload = {"type": type, **data}
        return f"data: {json.dumps(payload)}\n\n"
