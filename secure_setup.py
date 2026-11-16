"""Initialize the current Windows user's encryption key and API credential."""
from sensitive_data import SensitiveDataError, initialize_local_secrets


def main():
    try:
        created = initialize_local_secrets()
    except SensitiveDataError as error:
        raise SystemExit(str(error)) from None
    print("Local secrets created." if created else "Existing local secrets verified.")
    print("Stored under the current user's LOCALAPPDATA with Windows DPAPI protection.")
    print("Run the API, importer and automation under this same Windows account.")


if __name__ == "__main__":
    main()
