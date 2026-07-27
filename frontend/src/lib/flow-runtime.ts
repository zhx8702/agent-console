export type MessageFlowRuntimeConfig = {
  enabled?: boolean;
  name?: string;
  allowed?: boolean;
  reason?: string;
  allowed_names?: string[];
  allow_target_flows?: boolean;
  allow_compatible_fallback?: boolean;
};
