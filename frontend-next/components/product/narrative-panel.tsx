import { AlertTriangle } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { highlightRules } from "./rule-highlight";

export function NarrativePanel({ narrative, error }: { narrative?: string | null; error?: string | null }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Standing Narrative</CardTitle>
      </CardHeader>
      <CardContent>
        {error ? (
          <div className="callout error">
            <AlertTriangle size={16} />
            <span>{error}</span>
          </div>
        ) : (
          <p className="narrative">{narrative ? highlightRules(narrative) : "No narrative has been generated for this audit yet."}</p>
        )}
      </CardContent>
    </Card>
  );
}
