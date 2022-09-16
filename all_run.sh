# Write a for loop which loops over strings

for i in "sum" "prod"
do
  # Write a for loop which loops over integers from 2 to 4
  for j in 2 3 4
  do
    sbatch run.sh $i $j
  done
done