import { BACKEND_BASE } from "./events_const";

export interface IdentifyResult {
  identified: boolean;
  provider?: string;
  base_url?: string;
  kind?: string;
  default_model?: string;
  models?: string[];
}

// 粘贴 API Key 后自动识别：探测端点、默认模型与可用模型列表。
export async function identifyCredential(secret: string): Promise<IdentifyResult> {
  const resp = await fetch(`${BACKEND_BASE}/api/credentials/identify`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ secret }),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`/api/credentials/identify -> ${resp.status}: ${text.slice(0, 200)}`);
  }
  return resp.json() as Promise<IdentifyResult>;
}
