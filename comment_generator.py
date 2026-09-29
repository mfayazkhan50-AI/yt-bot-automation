import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# Initialize OpenAI client with OpenRouter base URL
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY"),
)


def generate_comment(prompt):
    response = client.chat.completions.create(
        model=os.getenv("LLM_MODEL", "openrouter/auto"),
        messages=[
            {"role": "system", "content": "You are a friendly YouTube viewer commenting on videos."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.7
    )
    return response.choices[0].message.content.strip()