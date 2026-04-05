# %%
import os
import pandas as pd
import time
from tqdm import tqdm
from datetime import datetime, timezone
import praw


# Initialize Reddit API client - write down your own
reddit = praw.Reddit(
    client_id="",
    client_secret="",
    user_agent=""
)

# %%
input_dir = "" # change to your path
csv_files = [f for f in os.listdir(input_dir) if f.endswith("_submissions_pushshift.csv")] 


for file_name in csv_files:
    file_path = os.path.join(input_dir, file_name)
    
    try:
        df = pd.read_csv(file_path)
    except Exception as e:
        print(f" Error reading {file_name}: {e}")
        continue

    if 'id' not in df.columns:
        print(f" Skipping {file_name}: no 'id' column.")
        continue

    submission_ids = df['id'].astype(str).tolist()
    records = []

    print(f"\n Processing file: {file_name} ({len(submission_ids)} rows)")

    for sub_id in tqdm(submission_ids, desc=f"{file_name}", unit="row"):
        attempt = 0
        success = False
        while attempt < 3 and not success:
            try:
                submission = reddit.submission(id=sub_id)
                records.append({
                    "title": submission.title,
                    "subreddit": submission.subreddit.display_name,
                    "author": str(submission.author),
                    "id": submission.id,
                    "created_utc": datetime.fromtimestamp(submission.created_utc, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                    "stickied": submission.stickied,
                    "score": submission.score,
                    "num_comments": submission.num_comments,
                    "num_crossposts": submission.num_crossposts
                })
                success = True
            except Exception as e:
                attempt += 1
                if attempt == 3:
                    tqdm.write(f" Failed to fetch ID {sub_id}: {e}")
                time.sleep(1)

    new_file_name = file_name.replace("_submissions_pushshift1", "_submissions_redditapi1")
    new_file_path = os.path.join(input_dir, new_file_name)
    pd.DataFrame(records).to_csv(new_file_path, index=False)
    print(f" Saved: {new_file_name}")
