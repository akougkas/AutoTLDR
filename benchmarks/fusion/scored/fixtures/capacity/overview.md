# Cluster Capacity Model

The formula model is [capacity.xlsx](capacity.xlsx), its interchange table is
[export.csv](export.csv), and the schema is [schema.xml](schema.xml). The missing
assumption register is [assumptions.md](assumptions.md). General spreadsheet
documentation at https://office.example/formulas is external.

The shared inputs are `node_count`, `per_node_mbps`, and `overhead_factor`.

safety_margin_pct = 12

rack_limit = 16

effective_capacity_mbps = 3000

The exact effective-capacity planning target above is a declaration. The
same-named workbook cell is a derived formula, not an independent constant.

The approximate rack limit was once described as about sixteen; this is not a
second exact assignment.
