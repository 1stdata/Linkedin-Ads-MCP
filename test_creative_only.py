from dotenv import load_dotenv
load_dotenv()
import creative_pipeline as cp
POST = "urn:li:share:7474912299171020801"  # already created in the prior run
try:
    cr = cp.create_creative("507196009", "799010234", POST, "LEARN_MORE", "DRAFT")
    print("\n=== CREATIVE CREATED ===")
    print("creative:", cr)
except Exception:
    import traceback; traceback.print_exc()
