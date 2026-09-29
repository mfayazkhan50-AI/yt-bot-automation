from googleapiclient.discovery import build
import random


def find_videos(api_key, keywords, max_results=30):
    youtube = build("youtube", "v3", developerKey=api_key)
    all_videos = []
    videos_per_keyword = max(1, max_results // len(keywords))

    for keyword in keywords:
        try:
            request = youtube.search().list(
                part="snippet",
                q=keyword,
                type="video",
                videoDuration="medium",
                relevanceLanguage="en",
                order="date",
                maxResults=videos_per_keyword,
            )
            response = request.execute()

            for item in response.get("items", []):
                video = {
                    "id": item["id"]["videoId"],
                    "title": item["snippet"]["title"],
                    "description": item["snippet"].get("description", ""),
                    "channel": item["snippet"]["channelTitle"],
                    "url": f"https://www.youtube.com/watch?v={item['id']['videoId']}",
                }
                all_videos.append(video)
        except Exception as e:
            print(f"[VIDEO FINDER] Error searching '{keyword}': {e}")

    random.shuffle(all_videos)
    selected = all_videos[:max_results]
    print(f"[VIDEO FINDER] Found {len(selected)} videos")
    return selected
