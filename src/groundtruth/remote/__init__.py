"""The compute server: the plugin on a laptop sends its Simulate, Suggest fixes, Thermal and Current capacity jobs
to `groundtruth serve` on a powerful PC on the local network (protocol.py: signed requests, restricted pickles;
server.py: the queue and the job processes; worker.py: one job; client.py: the plugin's side)."""
