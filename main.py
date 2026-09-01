import subprocess
import time

# Run the script and wait for it to finish
result = subprocess.run(["python", "scraper_cargills.py"], capture_output=True, text=True)

# Print the script's output
print("Output:", result.stdout)
print("Errors:", result.stderr)

time.sleep(1)

result = subprocess.run(["python", "scraper_glomark.py"], capture_output=True, text=True)

# Print the script's output
print("Output:", result.stdout)
print("Errors:", result.stderr)

time.sleep(1)

result = subprocess.run(["python", "scraper_keells.py"], capture_output=True, text=True)

# Print the script's output
print("Output:", result.stdout)
print("Errors:", result.stderr)

time.sleep(1)

result = subprocess.run(["python", "scraper_spar2u.py"], capture_output=True, text=True)

# Print the script's output
print("Output:", result.stdout)
print("Errors:", result.stderr)

time.sleep(1)

result = subprocess.run(["python", "match_products.py"], capture_output=True, text=True)

# Print the script's output
print("Output:", result.stdout)
print("Errors:", result.stderr)




