import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY"),
)

def generate_youtube_comment(video_title):
    prompt = f"Write a short, natural YouTube comment for a video titled: '{video_title}'. Keep it under 2 sentences."
    
    response = client.chat.completions.create(
        model=os.getenv("LLM_MODEL", "openrouter/auto"),
        messages=[
            {"role": "system", "content": "You are a friendly YouTube viewer."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.7
    )
    return response.choices[0].message.content.strip()

if __name__ == "__main__":
    comment = generate_youtube_comment("How to host Python bot on Ubuntu VPS")
    print("\nSUCCESS! Generated Comment:\n", comment)