-- Create the default Astrocyte AGE graph.
-- The graph name must match AgeGraphStore's `graph_name` constructor param
-- (default: 'astrocyte').  If you change the default, update this script.
LOAD 'age';
SET search_path = ag_catalog, "$user", public;

SELECT create_graph('astrocyte');
