export interface GraphNodeData {
  id: string;
  label: string;
  title: string;
  arxiv_id: string;
  influential_citation_count: number;
  citation_count?: number;
  influence_score: number;
  source_url: string;
  group: string;
}

export interface GraphEdgeData {
  source: string;
  target: string;
  relation: string;
  weight: number;
  is_perfunctory: boolean;
}

export interface GraphData {
  nodes: GraphNodeData[];
  edges: GraphEdgeData[];
}
