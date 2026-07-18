import { useMemo } from "react";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { AgentMetrics } from "../../api/types";
import { agentLabelIt } from "../../lib/labels";

interface AccuracyTrendChartProps {
  /** per_agent metrics from the most recent evaluation; each carries its own `trend` (last 8 evaluations). */
  perAgent: AgentMetrics[];
}

const AGENT_COLORS: Record<string, string> = {
  technical: "#599bff",
  fundamentals: "#f5b942",
  macro_news: "#a78bfa",
  corporate_news: "#fb923c",
  synthesizer: "#34d399",
  validator: "#fb7185",
};

function colorFor(agentName: string, idx: number): string {
  return AGENT_COLORS[agentName] ?? ["#599bff", "#f5b942", "#a78bfa", "#fb923c", "#34d399", "#fb7185"][idx % 6];
}

export default function AccuracyTrendChart({ perAgent }: AccuracyTrendChartProps) {
  const { data, agents } = useMemo(() => {
    const withTrend = perAgent.filter((a) => a.trend.length > 0);
    const maxLen = withTrend.reduce((acc, a) => Math.max(acc, a.trend.length), 0);
    if (maxLen === 0) return { data: [], agents: withTrend };

    const rows: Array<Record<string, number | string>> = [];
    for (let i = 0; i < maxLen; i += 1) {
      const row: Record<string, number | string> = {
        idx: i - maxLen + 1, // negative offsets from the most recent evaluation (0)
        label: i === maxLen - 1 ? "Attuale" : `V${i - maxLen + 1}`,
      };
      withTrend.forEach((agent) => {
        // Right-align: the last element of every trend array is the most recent evaluation.
        const offset = maxLen - agent.trend.length;
        if (i >= offset) {
          row[agent.agent_name] = Math.round(agent.trend[i - offset] * 1000) / 10; // -> percentage
        }
      });
      rows.push(row);
    }
    return { data: rows, agents: withTrend };
  }, [perAgent]);

  if (data.length === 0) {
    return (
      <div className="flex h-56 items-center justify-center rounded-xl border border-dashed border-slate-700 text-sm text-slate-500">
        Dati insufficienti: servono almeno due valutazioni settimanali per mostrare un andamento.
      </div>
    );
  }

  return (
    <ResponsiveContainer width="100%" height={280}>
      <LineChart data={data} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
        <CartesianGrid stroke="#232c40" strokeDasharray="3 3" vertical={false} />
        <XAxis dataKey="label" stroke="#8b95ab" tick={{ fontSize: 11 }} />
        <YAxis
          domain={[0, 100]}
          tickFormatter={(v: number) => `${v}%`}
          stroke="#8b95ab"
          tick={{ fontSize: 11 }}
          width={44}
        />
        <Tooltip
          formatter={(value: number, name: string) => [`${value.toFixed(0)}%`, agentLabelIt(name)]}
          contentStyle={{
            background: "rgba(18, 24, 38, 0.95)",
            border: "1px solid #232c40",
            borderRadius: 8,
            fontSize: 12,
          }}
        />
        <Legend
          formatter={(value: string) => agentLabelIt(value)}
          wrapperStyle={{ fontSize: 12, color: "#8b95ab" }}
        />
        {agents.map((agent, idx) => (
          <Line
            key={agent.agent_name}
            type="monotone"
            dataKey={agent.agent_name}
            name={agent.agent_name}
            stroke={colorFor(agent.agent_name, idx)}
            strokeWidth={2}
            dot={{ r: 3 }}
            connectNulls
            isAnimationActive={false}
          />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}
