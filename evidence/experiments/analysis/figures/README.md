# Figures — `experiments`

Rules: **one chart per file**, **no titles**, legends **outside** axes.

## Lifecycle
- `lifecycle_boxplot_by_profile`
- `lifecycle_boxplot_by_operation`
- `lifecycle_boxplot_<op>_by_profile` (6 files)
- `lifecycle_boxplot_stage_{handler,database,external,serialization}`
- `lifecycle_stacked_ops`, `lifecycle_stacked_cbrs`, `lifecycle_stacked_wide`

## Persistence / cold start / mTLS
- `persistence_boxplot_<N>_devices`, `persistence_wall_median`
- `coldstart_boxplot_{external,internal,by_profile}`
- `mtls_boxplot_{all,new_connection,persistent}`
- `mtls_overhead_compact.(csv|md)`
