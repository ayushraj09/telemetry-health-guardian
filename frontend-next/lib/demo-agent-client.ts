const DEMO_AGENT_API_BASE = process.env.NEXT_PUBLIC_DEMO_AGENT_API_URL ?? "http://localhost:8010";

export type DemoAgentRunRequest = {
  question: string;
  pdf_path?: string;
  chaos_mode: boolean;
  ollama_model?: string;
};

export async function runDemoAgent(body: DemoAgentRunRequest) {
  const response = await fetch(`${DEMO_AGENT_API_BASE}/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json();
}

export async function listOllamaModels(): Promise<string[]> {
  const response = await fetch(`${DEMO_AGENT_API_BASE}/ollama/models`);
  if (!response.ok) {
    return [];
  }
  return response.json();
}
