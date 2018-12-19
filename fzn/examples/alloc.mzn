int: nv = 5;
int: nr = 3;

array[1..nv] of 1..nr: init_pos = [1,1,1,2,3];

array[1..nr] of var int: opportunities = [1,3,1];

array[1..nr, 1..nr] of int: cost = array2d(1..nr, 1..nr, [0,2,1,2,0,2,1,2,0]);

array[1..nv] of var 1..nr: target_pos;

var int: opportunity_cost = sum([max(0, opportunities[r] - sum([bool2int(target_pos[v] = r) | v in 1..nv])) * 10 | r in 1..nr]);
var int: movement_cost = sum([cost[init_pos[v], target_pos[v]] | v in 1..nv]);

var int: total_cost;
constraint (
  total_cost = movement_cost + opportunity_cost
);

solve minimize total_cost;