"""Chat Completions adapter with one bounded, observable truncation retry.

Only the configured endpoint is used. Credentials and private reasoning are never
included in public call records. A truncated response never reaches tool execution.
"""
from copy import deepcopy
import json
import os
import socket
import time
from urllib import error, request
from urllib.parse import urlsplit

POLICY_VERSION = "gateway-stages-20260922"
WIRE_FORMAT = "gateway-extra-body-v1"


def thinking_parameters(profile, state, wire_format=WIRE_FORMAT):
    """Gateway JSON nesting is endpoint-specific, independent of thinking state."""
    if profile not in ("deepseek-json", "deepseek-low", "gateway-json", "gateway-low", "gateway-none"):
        return {}
    thinking = {"type": state}
    params = {"reasoning_effort": "low"}
    if profile.startswith("gateway-") and wire_format == WIRE_FORMAT:
        params["extra_body"] = {"thinking": thinking}
    else:
        params["thinking"] = thinking
    return params


class ModelError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def public_message(message):
    return {k:deepcopy(message[k]) for k in ("role", "content", "tool_calls") if k in message}

def public_payload(payload):
    result=deepcopy(payload)
    result["messages"]=[{k:v for k,v in m.items() if k!="reasoning_content"} for m in result.get("messages",[])]
    return result

def call_summary(model):
    call=getattr(model,"last_call",{}) or {}
    return {**call,"attempts":[{k:v for k,v in a.items() if k not in ("request","response")} for a in call.get("attempts",[])]}

def aggregate_usage(attempts):
    result={}
    for field in ("prompt_tokens","completion_tokens","total_tokens"):
        values=[a.get("usage",{}).get(field) for a in attempts]
        if values and all(type(v) is int and v>=0 for v in values):result[field]=sum(values)
    values=[a.get("usage",{}).get("completion_tokens_details",{}).get("reasoning_tokens") for a in attempts]
    if values and all(type(v) is int and v>=0 for v in values):result["completion_tokens_details"]={"reasoning_tokens":sum(values)}
    return result

class CompatibleModel:
    mode="live"
    protocol="chat_completions"
    def __init__(self,url=None,key=None,model=None,timeout=90,*,request_profile=None,
                 max_tokens=None,retry_max_tokens=None,retry_timeout=None,token_limit_field=None,wire_format=WIRE_FORMAT,gateway_cache_bypass=True):
        self.url=url or os.environ.get("FDE_MODEL_URL","")
        self.key=key or os.environ.get("FDE_MODEL_KEY","")
        self.model=model or os.environ.get("FDE_MODEL_NAME","")
        self.request_profile=request_profile or os.environ.get("FDE_MODEL_PROFILE","compatible")
        if wire_format not in (WIRE_FORMAT, "legacy-top-level"):
            raise ModelError("MODEL_WIRE_FORMAT_INVALID")
        self.wire_format = wire_format
        if type(gateway_cache_bypass) is not bool:raise ModelError("MODEL_CACHE_POLICY_INVALID")
        self.gateway_cache_bypass = gateway_cache_bypass
        if self.request_profile not in ("compatible","json-object","deepseek-json","gateway-json","deepseek-low","gateway-low"):
            raise ModelError("MODEL_PROFILE_INVALID")
        try:
            self.max_tokens=int(max_tokens if max_tokens is not None else os.environ.get("FDE_MODEL_MAX_TOKENS",8192))
            self.retry_max_tokens=int(retry_max_tokens if retry_max_tokens is not None else os.environ.get("FDE_MODEL_RETRY_MAX_TOKENS",self.max_tokens*2))
            self.timeout=float(os.environ.get("FDE_MODEL_TIMEOUT",timeout))
            self.retry_timeout=float(retry_timeout if retry_timeout is not None else os.environ.get("FDE_MODEL_RETRY_TIMEOUT",max(180,self.timeout)))
        except (ValueError,TypeError):raise ModelError("MODEL_POLICY_INVALID") from None
        self.token_limit_field=token_limit_field or os.environ.get("FDE_MODEL_TOKEN_LIMIT_FIELD","max_tokens")
        if not (0<self.max_tokens<self.retry_max_tokens<=1048576 and 0<self.timeout<=self.retry_timeout<=3600
                and self.token_limit_field in ("max_tokens","max_completion_tokens")):
            raise ModelError("MODEL_POLICY_INVALID")
        self.deadline=None
        self.last_call={}
        if not all((self.url,self.key,self.model)):raise ModelError("MODEL_NOT_CONFIGURED")
        parsed=urlsplit(self.url)
        local=parsed.hostname in ("localhost","127.0.0.1","::1")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:raise ModelError("MODEL_URL_INVALID")
        if parsed.scheme!="https" and not (parsed.scheme=="http" and local):raise ModelError("MODEL_URL_INVALID")
        if not parsed.hostname:raise ModelError("MODEL_URL_INVALID")

    def policy_config(self):
        return {"gateway_cache_bypass":self.gateway_cache_bypass,"wire_format":self.wire_format,"request_profile":self.request_profile,"max_tokens":self.max_tokens,
                "retry_max_tokens":self.retry_max_tokens,"timeout":self.timeout,
                "retry_timeout":self.retry_timeout,"token_limit_field":self.token_limit_field}

    def _payload(self,messages,tools):
        payload={"model":self.model,"messages":messages,"stream":False,self.token_limit_field:self.max_tokens}
        if tools:payload.update(tools=tools,tool_choice="auto")
        if self.request_profile in ("json-object","deepseek-json","gateway-json") and not tools:
            payload["response_format"]={"type":"json_object"}
        payload.update(thinking_parameters(self.request_profile, "enabled", self.wire_format))
        if self.request_profile.startswith("gateway-") and self.gateway_cache_bypass:
            # Observed same response ID for opposite switches: bypass response cache.
            payload["cache"] = {"no-cache":True,"no-store":True}
        return payload

    def _send(self,payload,timeout):
        req=request.Request(self.url,data=json.dumps(payload).encode(),headers={
            "Authorization":"Bearer "+self.key,"Content-Type":"application/json"})
        deadline=time.monotonic()+timeout
        with request.build_opener(NoRedirect).open(req,timeout=timeout) as response:
            headers={k.lower():v for k,v in getattr(response,"headers",{}).items()}
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError()
            sock=getattr(getattr(getattr(response,"fp",None),"raw",None),"_sock",None)
            if sock is not None:sock.settimeout(remaining)
            raw=response.read(2_000_001)
            if time.monotonic()>deadline:raise TimeoutError()
        if len(raw)>2_000_000:raise ModelError("MODEL_RESPONSE_TOO_LARGE")
        return json.loads(raw),{k:v for k,v in headers.items() if k in (
            "llm_provider-x-request-id","x-request-id","x-litellm-call-id","request-id","x-bce-request-id")}

    def complete(self,messages,tools):
        self.last_call={"policy_version":POLICY_VERSION,"attempts":[],"usage":{}}
        original=self._payload(messages,tools)
        field=next((f for f in ("max_tokens","max_completion_tokens") if f in original),None)
        for number in range(2):
            payload=deepcopy(original)
            if number:payload[field]=self.retry_max_tokens
            allowance=self.timeout if number==0 else self.retry_timeout
            if self.deadline is not None:allowance=min(allowance,self.deadline-time.monotonic())
            if allowance<=0:raise ModelError("MODEL_TURN_TIMEOUT")
            attempt={"attempt":number+1,"request":public_payload(payload),"timeout_seconds":round(allowance,3),
                     "max_tokens":payload.get(field),"usage":{},"trace_ids":{}}
            self.last_call["attempts"].append(attempt)
            started=time.monotonic()
            try:
                body,trace=self._send(payload,allowance)
                attempt["trace_ids"]=trace
                if isinstance(body.get("id"),str):attempt["response_id"]=body["id"]
                choice=body["choices"][0];message=choice["message"]
                if not isinstance(message,dict) or message.get("role")!="assistant":raise ValueError("invalid role")
                usage=body.get("usage") or {}
                if not isinstance(usage,dict):raise ValueError("invalid usage")
                attempt.update(http_status=200,finish_reason=choice.get("finish_reason"),usage=usage,response=public_message(message))
                finish=choice.get("finish_reason")
                if finish=="length":
                    generated=usage.get("completion_tokens")
                    if field and type(generated) is int and generated>=payload[field]:
                        attempt["error"]="MODEL_OUTPUT_TRUNCATED"
                        if number==0 and self.retry_max_tokens>payload[field]:continue
                        raise ModelError("MODEL_OUTPUT_TRUNCATED")
                    raise ModelError("MODEL_LENGTH_LIMIT_UNCONFIRMED")
                if finish not in (None,"stop","tool_calls"):
                    raise ModelError("MODEL_FINISH_"+str(finish).upper())
                clean={k:deepcopy(message[k]) for k in ("role","content","tool_calls","reasoning_content") if k in message}
                return clean,aggregate_usage(self.last_call["attempts"])
            except (TimeoutError,socket.timeout):
                attempt["error"]="MODEL_TIMEOUT";raise ModelError("MODEL_TIMEOUT") from None
            except error.HTTPError as exc:
                code="MODEL_HTTP_"+str(exc.code)
                attempt["http_status"]=exc.code
                attempt["trace_ids"]={k.lower():v for k,v in (exc.headers or {}).items() if k.lower() in ("x-request-id","x-bce-request-id","llm_provider-x-request-id","x-litellm-call-id")}
                exc.close();attempt["error"]=code;raise ModelError(code) from None
            except error.URLError as exc:
                code="MODEL_TIMEOUT" if isinstance(exc.reason,(TimeoutError,socket.timeout)) else "MODEL_NETWORK_ERROR"
                attempt["error"]=code;raise ModelError(code) from None
            except (ValueError,KeyError,IndexError,TypeError,AttributeError):
                attempt["error"]="MODEL_INVALID_RESPONSE";raise ModelError("MODEL_INVALID_RESPONSE") from None
            except ModelError as exc:
                attempt["error"]=exc.code;raise
            finally:
                attempt["seconds"]=round(time.monotonic()-started,3)
                self.last_call["usage"]=aggregate_usage(self.last_call["attempts"])

class AnthropicModel(CompatibleModel):
    """Messages API adapter for Anthropic-compatible endpoints (FDE_MODEL_PROTOCOL=anthropic).

    The product runtime speaks Chat Completions internally; this adapter converts
    messages, tools and usage to the Messages wire format and back. Tool calls are
    carried as function calls in the internal shape so the Agent graph is unchanged.
    """
    protocol="anthropic_messages"
    anthropic_version="2023-06-01"

    def __init__(self,url=None,**kwargs):
        super().__init__(url=url,**kwargs)
        self.url=self.url.rstrip("/")
        if not self.url.endswith("/messages"):self.url+="/v1/messages"

    def _payload(self,messages,tools):
        system=[];converted=[]
        def add(role,blocks):
            if not blocks:return
            if converted and converted[-1]["role"]==role:converted[-1]["content"].extend(blocks)
            else:converted.append({"role":role,"content":list(blocks)})
        for message in messages:
            role=message.get("role")
            if role=="system":
                if message.get("content"):system.append(message["content"])
            elif role=="user":
                add("user",[{"type":"text","text":message.get("content") or ""}])
            elif role=="assistant":
                blocks=[]
                if message.get("content"):blocks.append({"type":"text","text":message["content"]})
                for call in message.get("tool_calls") or []:
                    function=call.get("function",{})
                    try:arguments=json.loads(function.get("arguments") or "{}")
                    except ValueError:arguments={}
                    blocks.append({"type":"tool_use","id":call.get("id") or "call_invalid",
                                   "name":function.get("name") or "unknown","input":arguments})
                add("assistant",blocks)
            elif role=="tool":
                block={"type":"tool_result","tool_use_id":message.get("tool_call_id") or "invalid",
                       "content":message.get("content") or ""}
                try:
                    data=json.loads(block["content"])
                    if isinstance(data,dict) and data.get("status")=="error":block["is_error"]=True
                except ValueError:pass
                add("user",[block])
        payload={"model":self.model,"max_tokens":self.max_tokens,"messages":converted}
        if system:payload["system"]="\n\n".join(system)
        if tools:
            payload["tools"]=[{"name":t["function"]["name"],
                               "description":t["function"].get("description",""),
                               "input_schema":t["function"]["parameters"]} for t in tools]
            payload["tool_choice"]={"type":"auto"}
        return payload

    def _send(self,payload,timeout):
        req=request.Request(self.url,data=json.dumps(payload).encode(),headers={
            "x-api-key":self.key,"anthropic-version":self.anthropic_version,"Content-Type":"application/json"})
        deadline=time.monotonic()+timeout
        with request.build_opener(NoRedirect).open(req,timeout=timeout) as response:
            headers={k.lower():v for k,v in getattr(response,"headers",{}).items()}
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError()
            sock=getattr(getattr(getattr(response,"fp",None),"raw",None),"_sock",None)
            if sock is not None:sock.settimeout(remaining)
            raw=response.read(2_000_001)
            if time.monotonic()>deadline:raise TimeoutError()
        if len(raw)>2_000_000:raise ModelError("MODEL_RESPONSE_TOO_LARGE")
        return json.loads(raw),{k:v for k,v in headers.items() if k in (
            "request-id","x-request-id","llm_provider-x-request-id","x-litellm-call-id","x-bce-request-id")}

    def complete(self,messages,tools):
        self.last_call={"policy_version":POLICY_VERSION,"attempts":[],"usage":{}}
        original=self._payload(messages,tools)
        for number in range(2):
            payload=deepcopy(original)
            if number:payload["max_tokens"]=self.retry_max_tokens
            allowance=self.timeout if number==0 else self.retry_timeout
            if self.deadline is not None:allowance=min(allowance,self.deadline-time.monotonic())
            if allowance<=0:raise ModelError("MODEL_TURN_TIMEOUT")
            attempt={"attempt":number+1,"request":public_payload(payload),"timeout_seconds":round(allowance,3),
                     "max_tokens":payload["max_tokens"],"usage":{},"trace_ids":{}}
            self.last_call["attempts"].append(attempt)
            started=time.monotonic()
            try:
                body,trace=self._send(payload,allowance)
                attempt["trace_ids"]=trace
                if isinstance(body.get("id"),str):attempt["response_id"]=body["id"]
                content=body.get("content")
                if not isinstance(content,list):raise ValueError("invalid content")
                texts=[];calls=[];thinking=[]
                for block in content:
                    if not isinstance(block,dict):raise ValueError("invalid block")
                    if block.get("type")=="text":texts.append(block.get("text") or "")
                    elif block.get("type")=="tool_use":
                        calls.append({"id":block.get("id"),"type":"function",
                                      "function":{"name":block.get("name"),
                                                  "arguments":json.dumps(block.get("input") or {},ensure_ascii=False)}})
                    elif block.get("type")=="thinking":thinking.append(block.get("thinking") or "")
                stop=body.get("stop_reason");usage=body.get("usage") or {}
                if not isinstance(usage,dict):raise ValueError("invalid usage")
                normalized={}
                if type(usage.get("input_tokens")) is int:normalized["prompt_tokens"]=usage["input_tokens"]
                if type(usage.get("output_tokens")) is int:normalized["completion_tokens"]=usage["output_tokens"]
                if "prompt_tokens" in normalized and "completion_tokens" in normalized:
                    normalized["total_tokens"]=normalized["prompt_tokens"]+normalized["completion_tokens"]
                message={"role":"assistant","content":"".join(texts)}
                if calls:message["tool_calls"]=calls
                if thinking:message["reasoning_content"]="".join(thinking)
                attempt.update(http_status=200,finish_reason=stop,usage=normalized,
                               response=public_message(message))
                if stop=="max_tokens":
                    generated=normalized.get("completion_tokens")
                    if type(generated) is int and generated>=payload["max_tokens"]:
                        attempt["error"]="MODEL_OUTPUT_TRUNCATED"
                        if number==0 and self.retry_max_tokens>payload["max_tokens"]:continue
                        raise ModelError("MODEL_OUTPUT_TRUNCATED")
                    raise ModelError("MODEL_LENGTH_LIMIT_UNCONFIRMED")
                if stop not in (None,"end_turn","stop_sequence","tool_use"):
                    raise ModelError("MODEL_FINISH_"+str(stop).upper())
                return message,aggregate_usage(self.last_call["attempts"])
            except (TimeoutError,socket.timeout):
                attempt["error"]="MODEL_TIMEOUT";raise ModelError("MODEL_TIMEOUT") from None
            except error.HTTPError as exc:
                code="MODEL_HTTP_"+str(exc.code)
                attempt["http_status"]=exc.code
                attempt["trace_ids"]={k.lower():v for k,v in (exc.headers or {}).items() if k.lower() in ("x-request-id","x-bce-request-id","request-id","llm_provider-x-request-id","x-litellm-call-id")}
                exc.close();attempt["error"]=code;raise ModelError(code) from None
            except error.URLError as exc:
                code="MODEL_TIMEOUT" if isinstance(exc.reason,(TimeoutError,socket.timeout)) else "MODEL_NETWORK_ERROR"
                attempt["error"]=code;raise ModelError(code) from None
            except (ValueError,KeyError,IndexError,TypeError,AttributeError):
                attempt["error"]="MODEL_INVALID_RESPONSE";raise ModelError("MODEL_INVALID_RESPONSE") from None
            except ModelError as exc:
                attempt["error"]=exc.code;raise
            finally:
                attempt["seconds"]=round(time.monotonic()-started,3)
                self.last_call["usage"]=aggregate_usage(self.last_call["attempts"])


def create_model(timeout=90,**kwargs):
    """Select the wire adapter from FDE_MODEL_PROTOCOL; default keeps Chat Completions."""
    protocol=os.environ.get("FDE_MODEL_PROTOCOL","chat_completions").strip().lower()
    if protocol in ("chat_completions","openai","compatible"):return CompatibleModel(timeout=timeout,**kwargs)
    if protocol in ("anthropic","anthropic_messages","anthropic-messages","messages"):return AnthropicModel(timeout=timeout,**kwargs)
    raise ModelError("MODEL_PROTOCOL_INVALID")


class ScriptedModel:
    """Deterministic teaching fixture, never evidence of live Agent behavior."""
    mode="scripted"
    def __init__(self,turns):self.turns=iter(deepcopy(turns))
    def complete(self,messages,tools):
        try:turn=next(self.turns)
        except StopIteration:raise ModelError("SCRIPT_EXHAUSTED") from None
        if isinstance(turn,str):raise ModelError(turn)
        return turn,{}
