from google import genai

genai.configure(api_key = "AIzaSyBLNeKjxD1wZoUn91tcNCSAb563Lm13490")

for m in genai.list_models():
    if 'generateContent' in m.supported_generation_methods:
        print(m.name)
