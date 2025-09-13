from utils.token_manager import get_token
from utils.upload_materials_to_meta_and_update_registry import upload_materials_to_meta_and_update_registry

if __name__ == "__main__":
    token = get_token()
    upload_materials_to_meta_and_update_registry(token)
    print("✅ media_registry.json пересоздан")