"""
Database Migration: Add WhatsApp Support
Run this ONCE before deploying to add new columns to Lead table
"""

from app import app, db
from sqlalchemy import text

def migrate_whatsapp_fields():
    """Add WhatsApp and contact preference fields to Lead table"""
    with app.app_context():
        try:
            # Check if columns already exist
            from sqlalchemy import inspect
            inspector = inspect(db.engine)
            columns = [col['name'] for col in inspector.get_columns('lead')]
            
            # Add whatsapp_number column
            if 'whatsapp_number' not in columns:
                db.session.execute(text(
                    "ALTER TABLE lead ADD COLUMN whatsapp_number VARCHAR(50);"
                ))
                print("✅ Added whatsapp_number column")
            else:
                print("⚠️ whatsapp_number column already exists")
            
            # Add contact_preference column
            if 'contact_preference' not in columns:
                db.session.execute(text(
                    "ALTER TABLE lead ADD COLUMN contact_preference VARCHAR(20) DEFAULT 'email';"
                ))
                print("✅ Added contact_preference column")
            else:
                print("⚠️ contact_preference column already exists")
            
            db.session.commit()
            print("✅ WhatsApp migration completed successfully!")
            
        except Exception as e:
            print(f"❌ Migration error: {e}")
            db.session.rollback()

if __name__ == "__main__":
    migrate_whatsapp_fields()